"""RemoteOK — 远程岗位站。

    GET https://remoteok.com/api             # 全量，一次 100 条
    GET https://remoteok.com/api?tags=web3   # 按标签过滤

实测要点：
- 无分页，单次固定返回约 100 条，且**只覆盖最近 3 天左右**（实测 07-22 ~ 07-25）。
  窗口极小，必须高频轮询，漏采无法回补。
- 响应数组**第一个元素不是岗位**，而是 `{last_updated, legal}` 说明对象，必须跳过。
- `salary_min`/`salary_max` 大量是 `0`（实测既出现过数字也出现过字符串 `"0"`），
  表示「未知」而非零薪；models.clean_money 会把 <=0 的值转成 NULL。
  **币种与周期要跟着清洗后的金额走** —— 字符串 `"0"` 在 Python 里是真值，
  照着原始字段判真假会写出「金额 NULL、币种却是 USD」的记录。
- robots：`User-agent: *` 是 `Allow: /` + `Crawl-delay: 1`；
  那几条 `Disallow: /*?action=get_jobs` 挂在 AhrefsBot 组下，对 `*` 不生效。
- ToS 要求 dofollow 回链 + 署名；logo 是注册商标，不可使用。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from ..models import Job, clean_money, ts_from_seconds
from .base import Source

log = logging.getLogger(__name__)

API = "https://remoteok.com/api"

# 单次只有 100 条窗口，用标签切片扩大覆盖面。偏向 Web3 / 远程技术岗。
TAGS = [
    None,  # 不带过滤的默认窗口
    "web3", "crypto", "blockchain", "solidity", "ethereum", "defi", "nft",
    "engineer", "dev", "senior", "backend", "frontend", "full stack",
    "design", "marketing", "product", "data",
]


class RemoteOkSource(Source):
    name = "remoteok"
    delay = 1.0  # robots 声明 Crawl-delay: 1

    def fetch(self, *, full: bool = False) -> AsyncIterator[Job]:
        return self._iter(full=full)

    async def _iter(self, *, full: bool) -> AsyncIterator[Job]:
        watermark = 0 if full else int(self.cursor.get("max_epoch") or 0)
        highest = watermark
        seen: set[str] = set()

        for tag in TAGS:
            params = {"tags": tag} if tag else None
            try:
                payload = await self.fetcher.get_json(API, params=params)
            except Exception as exc:  # noqa: BLE001 — 单个标签失败不该中断整轮
                log.warning("remoteok 标签 %r 拉取失败: %s", tag, exc)
                continue
            if not isinstance(payload, list):
                continue

            count = 0
            for item in payload:
                # 首元素是 {last_updated, legal} 说明对象，没有 id
                if not isinstance(item, dict) or not item.get("id"):
                    continue
                job_id = str(item["id"])
                if job_id in seen:
                    continue
                seen.add(job_id)

                epoch = int(item.get("epoch") or 0)
                highest = max(highest, epoch)
                # 窗口内的旧记录直接跳过（不能 break：不同标签的结果各自独立排序）
                if watermark and epoch and epoch <= watermark:
                    continue

                job = self._to_job(item)
                if job:
                    count += 1
                    yield job
            log.info("remoteok 标签 %-12r 新增 %d 条", tag, count)

        self.next_cursor = {"max_epoch": highest}

    def _to_job(self, item: dict) -> Job | None:
        title = (item.get("position") or "").strip()
        if not title:
            return None

        location = (item.get("location") or "").strip()

        # 币种和周期必须跟着**清洗后**的金额走。直接判 item["salary_min"] 的真假
        # 会踩坑：该字段实测出现过字符串 "0"（表示未知），而 "0" 在 Python 里是
        # 真值，于是写出「金额 NULL、币种却是 USD」的记录。
        smin = clean_money(item.get("salary_min"))
        smax = clean_money(item.get("salary_max"))
        has_salary = smin is not None or smax is not None

        return Job(
            source=self.name,
            source_id=str(item["id"]),
            url=item.get("url") or item.get("apply_url"),
            title=title,
            description=item.get("description"),
            company_name=(item.get("company") or "").strip() or None,
            company_logo=item.get("company_logo") or item.get("logo") or None,
            remote_type="remote",  # 站点只收远程岗
            locations=[p.strip() for p in location.split(",") if p.strip()],
            tags=[t for t in (item.get("tags") or []) if t],
            salary_min=smin,
            salary_max=smax,
            salary_currency="USD" if has_salary else None,
            salary_period="annual" if has_salary else None,
            posted_at=ts_from_seconds(item.get("epoch")),
            raw=item,
        )
