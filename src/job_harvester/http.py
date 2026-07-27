"""HTTP 客户端：限速 + 重试 + 请求计数。"""

from __future__ import annotations

import asyncio
import json
import logging
import random

import httpx

log = logging.getLogger(__name__)

# 403 也重试：web3.career 会偶发 403（实测 175 次请求中 1 次），
# 属于瞬时拦截而非永久封禁。重试次数有上限，真被封时仍会及时失败。
#
# 520~527 是 Cloudflare 自己的一族错误（源站连不上/握手失败/超时），
# 不是源站真的返回了这些码，重试通常就好。这条是踩出来的：Himalayas 全量
# 跑到第 198 次请求时收到一个 520，因为不在重试集合里直接抛异常，
# 把一轮预计三小时、上万次请求的回填整个中断掉。
RETRY_STATUS = {403, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524, 525, 526, 527}


class Fetcher:
    """按源限速的异步取数器。

    Himalayas 等站不暴露 RateLimit 响应头，阈值不可知，
    因此采用固定间隔 + 抖动的保守策略，而非探测式加速。
    """

    def __init__(
        self,
        *,
        user_agent: str,
        delay: float = 1.0,
        timeout: float = 30.0,
        max_retries: int = 4,
        proxy: str | None = None,
    ) -> None:
        self.delay = delay
        self.max_retries = max_retries
        self.requests = 0
        self._lock = asyncio.Lock()
        self._client = httpx.AsyncClient(
            headers={
                "User-Agent": user_agent,
                "Accept": "application/json, text/html;q=0.9,*/*;q=0.8",
                "Accept-Language": "en,zh-CN;q=0.9",
            },
            timeout=timeout,
            follow_redirects=True,
            proxy=proxy,
            # 免费代理池是按连接轮换出口的，复用连接会把出口钉死，抵消轮换
            limits=httpx.Limits(max_keepalive_connections=0) if proxy else httpx.Limits(),
        )
        if proxy:
            log.info("经代理出口: %s", proxy)

    async def __aenter__(self) -> "Fetcher":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    async def _throttle(self) -> None:
        # 串行化间隔，保证同一源的请求不会并发突刺
        async with self._lock:
            await asyncio.sleep(self.delay * random.uniform(0.85, 1.25))

    async def get(self, url: str, **kwargs) -> httpx.Response:
        last_exc: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            await self._throttle()
            self.requests += 1
            try:
                resp = await self._client.get(url, **kwargs)
            except httpx.HTTPError as exc:
                last_exc = exc
                log.warning("请求异常 %s (第 %d 次): %s", url, attempt, exc)
            else:
                if resp.status_code in RETRY_STATUS:
                    wait = self._retry_after(resp, attempt)
                    log.warning(
                        "HTTP %s %s，%.1fs 后重试 (第 %d 次)",
                        resp.status_code, url, wait, attempt,
                    )
                    await asyncio.sleep(wait)
                    continue
                resp.raise_for_status()
                return resp
            await asyncio.sleep(min(2**attempt, 30))

        raise RuntimeError(f"重试 {self.max_retries} 次仍失败: {url}") from last_exc

    async def get_json(self, url: str, **kwargs) -> dict | list:
        """取 JSON，并对「响应被截断」单独重试。

        实测 Himalayas 全量跑到第 164 个请求时收到截断的响应体
        （`Unterminated string ... char 67007`）。这类失败在 HTTP 层是 200，
        只有解析时才暴露；不在这里重试的话，一次抖动就会中断数小时的采集。
        """
        last_exc: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            resp = await self.get(url, **kwargs)
            try:
                return resp.json()
            except (json.JSONDecodeError, ValueError) as exc:
                last_exc = exc
                log.warning(
                    "响应体解析失败（可能被截断，%d 字节）%s，第 %d 次重试: %s",
                    len(resp.content), url, attempt, exc,
                )
                await asyncio.sleep(min(2**attempt, 30))
        raise RuntimeError(f"JSON 解析连续 {self.max_retries} 次失败: {url}") from last_exc

    async def post_json(self, url: str, payload: dict, **kwargs) -> dict:
        """Notion 的 loadPageChunk / queryCollection 需要 POST。"""
        await self._throttle()
        self.requests += 1
        resp = await self._client.post(url, json=payload, **kwargs)
        resp.raise_for_status()
        return resp.json()

    @staticmethod
    def _retry_after(resp: httpx.Response, attempt: int) -> float:
        header = resp.headers.get("Retry-After")
        if header:
            try:
                return float(header)
            except ValueError:
                pass
        return min(2**attempt, 60)
