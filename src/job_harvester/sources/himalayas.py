"""Himalayas — 全远程岗位站，体量最大的源（约 96,600 条）。

    GET https://himalayas.app/jobs/api?limit=20&offset=N

实测要点：
- `limit` 被服务端硬顶在 20：传 100/500/1000 均静默返回 20 条并回显 limit:20。
- **`limit >= 10` 时接口会把 `companyName` 换成字面量 "name"、
  `companyLogo` 换成 "thumbnail_url"**，其余字段（guid/title/薪资/分类/地域）仍然真实。
  这是刻意的批量抓取降级，且不报错、不改状态码，只能靠比对发现。
  阈值实测：limit 1~9 正常，10 及以上开始占位。故这里取 9。
  代价是全量请求数从 4,831 升到约 10,730，但保证公司名可用。
- 结果严格按 `pubDate` 倒序，`guid` 唯一且等于规范 URL，可直接做去重键。
- Cloudflare 前置，不暴露任何 RateLimit 响应头，阈值不可知 → 固定间隔保守请求。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from ..models import Job, ts_from_seconds
from .base import Source

log = logging.getLogger(__name__)

API = "https://himalayas.app/jobs/api"
# 服务端硬上限是 20，但 >=10 会返回占位公司名，故取降级前的最大值 9
PAGE_LIMIT = 9
# 接口在批量降级时写入的字面量，出现即说明 PAGE_LIMIT 被调高到了阈值以上
PLACEHOLDER_COMPANY = "name"
PLACEHOLDER_LOGO = "thumbnail_url"

_EMPLOYMENT = {
    "Full Time": "full-time",
    "Part Time": "part-time",
    "Contract": "contract",
    "Internship": "internship",
    "Temporary": "temporary",
    "Freelance": "freelance",
}


class HimalayasSource(Source):
    name = "himalayas"
    delay = 1.0  # 无 RateLimit 头，取值偏保守

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._warned_placeholder = False

    def fetch(self, *, full: bool = False) -> AsyncIterator[Job]:
        return self._iter(full=full)

    async def _iter(self, *, full: bool) -> AsyncIterator[Job]:
        watermark = 0 if full else int(self.cursor.get("max_pub_date") or 0)
        highest = watermark
        offset = 0
        total: int | None = None

        while True:
            payload = await self.fetcher.get_json(
                API, params={"limit": PAGE_LIMIT, "offset": offset}
            )
            if not isinstance(payload, dict):
                log.warning("himalayas 返回非预期结构，终止")
                break

            jobs = payload.get("jobs") or []
            if not jobs:
                break
            if total is None:
                total = payload.get("totalCount")
                log.info("himalayas totalCount=%s", total)

            stop = False
            for item in jobs:
                pub = int(item.get("pubDate") or 0)
                highest = max(highest, pub)
                # 严格倒序，遇到不新于水位线的即可停
                if watermark and pub and pub <= watermark:
                    stop = True
                    break
                job = self._to_job(item)
                if job:
                    yield job

            if stop:
                log.info("himalayas 触及水位线，提前结束于 offset=%s", offset)
                break

            offset += PAGE_LIMIT
            if total and offset >= total:
                break
            if offset and offset % (PAGE_LIMIT * 50) == 0:
                log.info("himalayas 进度 offset=%s / %s", offset, total)

        self.next_cursor = {"max_pub_date": highest}

    def _to_job(self, item: dict) -> Job | None:
        guid = item.get("guid")
        title = (item.get("title") or "").strip()
        if not guid or not title:
            return None

        tags = list(item.get("categories") or []) + list(item.get("parentCategories") or [])

        # 站点若调整降级阈值，占位值不能被静默写进库
        company = (item.get("companyName") or "").strip()
        logo = item.get("companyLogo") or None
        if company == PLACEHOLDER_COMPANY:
            if not self._warned_placeholder:
                log.error(
                    "himalayas 返回占位公司名，说明 PAGE_LIMIT=%d 已达降级阈值，"
                    "公司名与 logo 将置空。请调低 PAGE_LIMIT。",
                    PAGE_LIMIT,
                )
                self._warned_placeholder = True
            company = ""
        if logo == PLACEHOLDER_LOGO:
            logo = None

        return Job(
            source=self.name,
            source_id=str(guid),
            url=item.get("applicationLink") or str(guid),
            title=title,
            description=item.get("description") or item.get("excerpt"),
            company_name=company or None,
            company_logo=logo,
            employment_type=_EMPLOYMENT.get(
                item.get("employmentType"), item.get("employmentType")
            ),
            # 该站只收全远程岗位
            remote_type="remote",
            seniority=list(item.get("seniority") or []),
            locations=list(item.get("locationRestrictions") or []),
            tags=tags,
            salary_min=item.get("minSalary"),
            salary_max=item.get("maxSalary"),
            salary_currency=item.get("currency"),
            salary_period=item.get("salaryPeriod"),
            posted_at=ts_from_seconds(item.get("pubDate")),
            expires_at=ts_from_seconds(item.get("expiryDate")),
            raw=item,
        )
