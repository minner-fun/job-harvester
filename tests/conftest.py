"""回放测试的公共设施。

这些测试**不发任何网络请求**：`FakeFetcher` 按 URL 回放 `tests/fixtures/` 下的
样本，所以跑得快、结果稳定，源站改版也不会让测试变成随机失败。

fixture 里的值都是合成的（公司名、邮箱、钱包地址一律是编造的），
但**结构严格照抄真实响应**——字段名、嵌套层级，以及各站那些反直觉的坑
（Himalayas 的占位公司名、RemoteOK 的首元素说明对象、dejob 把 PII 藏在
`user` 对象里等）都原样保留。本仓库是公开的，真实响应里的招聘方联系方式
不能进 git，所以宁可自己编数据，也不直接倒一份线上样本进来。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def load(*parts: str):
    """读一个 fixture。`.json` 自动反序列化，其余（.xml/.html）返回文本。"""
    path = FIXTURES.joinpath(*parts)
    text = path.read_text(encoding="utf-8")
    return json.loads(text) if path.suffix == ".json" else text


class FakeResponse:
    """只实现 Source 会用到的那部分 httpx.Response。"""

    def __init__(self, payload) -> None:
        self._payload = payload

    @property
    def text(self) -> str:
        if isinstance(self._payload, str):
            return self._payload
        return json.dumps(self._payload, ensure_ascii=False)

    def json(self):
        if isinstance(self._payload, str):
            return json.loads(self._payload)
        return self._payload


class FakeFetcher:
    """按 URL 回放 fixture 的取数器替身。

    刻意**不继承**真的 `Fetcher`：继承会把 httpx.AsyncClient 和 `delay` 秒的
    限速一并带进来，测试会慢到没法跑，也不再是纯回放。这里只实现
    `Source` 真正用到的三个方法。

    `routes` 的值可以是：
    - 直接的 payload（dict / list / str）
    - `callable(params) -> payload`，用于按分页参数返回不同的页
    - `Exception` 实例，用于测「单个切片失败不该中断整轮」这类容错路径
    """

    def __init__(self, routes: dict[str, object] | None = None) -> None:
        self.routes: dict[str, object] = dict(routes or {})
        self.requests = 0
        #: 每次调用记一笔 (url, params)，供测试断言请求次数与翻页轨迹
        self.calls: list[tuple[str, dict | None]] = []

    def _resolve(self, url: str, params: dict | None):
        self.requests += 1
        self.calls.append((url, params))
        if url not in self.routes:
            raise AssertionError(f"FakeFetcher 没有为 {url} 配置 fixture；已配置: {list(self.routes)}")
        handler = self.routes[url]
        if isinstance(handler, Exception):
            raise handler
        if isinstance(handler, Callable):
            return handler(params or {})
        return handler

    async def get(self, url: str, **kwargs) -> FakeResponse:
        return FakeResponse(self._resolve(url, kwargs.get("params")))

    async def get_json(self, url: str, **kwargs):
        payload = self._resolve(url, kwargs.get("params"))
        return json.loads(payload) if isinstance(payload, str) else payload

    async def post_json(self, url: str, payload: dict, **kwargs):
        return self._resolve(url, payload)

    async def close(self) -> None:
        pass


async def collect(source, *, full: bool = False) -> list:
    """把 `Source.fetch()` 这个异步生成器抽干成 list。"""
    return [record async for record in source.fetch(full=full)]


@pytest.fixture
def fetcher() -> FakeFetcher:
    return FakeFetcher()
