"""Fetcher：哪些状态码该重试、以及响应被截断时的重试。

这两条都是踩出来的，不是照着 RFC 想出来的：

- Himalayas 全量跑到第 198 次请求时收到 Cloudflare 的 **520**。
  520~527 是 Cloudflare 自己的一族错误（源站连不上、握手失败、超时），
  不代表源站真的返回了这些码。当时不在重试集合里，一个瞬时错误就把
  一轮上万次请求、预计三小时的回填整个中断掉。
- 同一个源在第 164 次请求时收到**截断的响应体**。这类失败在 HTTP 层是 200，
  只有 `resp.json()` 时才暴露，所以必须单独在 get_json 里重试。
"""

from __future__ import annotations

import httpx
import pytest

from job_harvester.http import RETRY_STATUS, Fetcher


@pytest.fixture
def 无限速(monkeypatch):
    """把限速和退避都拿掉，否则单个用例要等几十秒。"""

    async def 立即返回(*args, **kwargs):
        return None

    monkeypatch.setattr("job_harvester.http.asyncio.sleep", 立即返回)


def 构造(handler, **kwargs) -> Fetcher:
    fetcher = Fetcher(user_agent="test", delay=0, **kwargs)
    fetcher._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return fetcher


@pytest.mark.parametrize("status", sorted(RETRY_STATUS))
async def test_可重试状态码会重试(无限速, status):
    次数 = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        次数["n"] += 1
        if 次数["n"] == 1:
            return httpx.Response(status)
        return httpx.Response(200, json={"ok": True})

    fetcher = 构造(handler)
    try:
        assert await fetcher.get_json("https://example.invalid/api") == {"ok": True}
    finally:
        await fetcher.close()

    assert 次数["n"] == 2


@pytest.mark.parametrize("status", [520, 521, 522, 523, 524, 525, 526, 527])
def test_cloudflare_52x_必须在重试集合里(status):
    """回归护栏：这一族被移出去，长时间全量就会被一次瞬时抖动打断。"""
    assert status in RETRY_STATUS


async def test_不可重试的状态码直接失败(无限速):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    fetcher = 构造(handler)
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await fetcher.get("https://example.invalid/api")
    finally:
        await fetcher.close()


async def test_重试用尽后抛错(无限速):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    fetcher = 构造(handler, max_retries=3)
    try:
        with pytest.raises(RuntimeError, match="重试 3 次仍失败"):
            await fetcher.get("https://example.invalid/api")
    finally:
        await fetcher.close()

    assert fetcher.requests == 3


async def test_响应体被截断时重试(无限速):
    """HTTP 层是 200，只有解析时才暴露，必须在 get_json 里单独重试。"""
    次数 = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        次数["n"] += 1
        if 次数["n"] == 1:
            return httpx.Response(200, text='{"jobs": [{"title": "被截断')
        return httpx.Response(200, json={"jobs": []})

    fetcher = 构造(handler)
    try:
        assert await fetcher.get_json("https://example.invalid/api") == {"jobs": []}
    finally:
        await fetcher.close()

    assert 次数["n"] == 2


async def test_遵守_Retry_After(无限速, monkeypatch):
    等待 = []

    async def 记录(seconds):
        等待.append(seconds)

    monkeypatch.setattr("job_harvester.http.asyncio.sleep", 记录)

    次数 = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        次数["n"] += 1
        if 次数["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "7"})
        return httpx.Response(200, json={})

    fetcher = 构造(handler)
    try:
        await fetcher.get("https://example.invalid/api")
    finally:
        await fetcher.close()

    assert 7.0 in 等待


async def test_请求计数():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    fetcher = 构造(handler)
    try:
        for _ in range(3):
            await fetcher.get("https://example.invalid/api")
    finally:
        await fetcher.close()

    assert fetcher.requests == 3
