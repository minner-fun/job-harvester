"""Jobicy — 远程岗位站。

    GET https://jobicy.com/api/v2/remote-jobs?count=100&geo=&industry=&tag=

实测要点：
- `count` 上限 100（传 200 仍返回 100），**没有 offset/page 参数**，
  所以是滚动窗口，只能靠 geo / industry / tag 组合切片扩大覆盖面。
- 官方文档 https://jobi.cy/apidocs
- ToS：需署名，且申请按钮要指向 feed 里的原始 URL。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import datetime

from ..models import Job
from .base import Source

log = logging.getLogger(__name__)

API = "https://jobicy.com/api/v2/remote-jobs"
COUNT = 100

# 无 offset，只能多维度切片。地域 + 行业两组即可覆盖大部分存量。
GEOS = [None, "usa", "europe", "uk", "canada", "asia", "australia", "latin-america"]
INDUSTRIES = [
    "engineering", "marketing", "design", "data-science",
    "business", "finance-legal", "supporting", "management",
]

_EMPLOYMENT = {
    "full-time": "full-time",
    "part-time": "part-time",
    "contract": "contract",
    "freelance": "freelance",
    "internship": "internship",
    "temporary": "temporary",
}


class JobicySource(Source):
    name = "jobicy"
    delay = 1.0

    def fetch(self, *, full: bool = False) -> AsyncIterator[Job]:
        return self._iter(full=full)

    async def _iter(self, *, full: bool) -> AsyncIterator[Job]:
        watermark = str(self.cursor.get("max_pub_date") or "") if not full else ""
        highest = watermark
        seen: set[str] = set()

        queries: list[dict] = [{"count": COUNT}]
        queries += [{"count": COUNT, "geo": g} for g in GEOS if g]
        queries += [{"count": COUNT, "industry": i} for i in INDUSTRIES]

        for params in queries:
            label = params.get("geo") or params.get("industry") or "default"
            try:
                payload = await self.fetcher.get_json(API, params=params)
            except Exception as exc:  # noqa: BLE001 — 单个切片失败不该中断整轮
                log.warning("jobicy 切片 %s 失败: %s", label, exc)
                continue
            if not isinstance(payload, dict):
                continue

            count = 0
            for item in payload.get("jobs") or []:
                job_id = str(item.get("id") or "")
                if not job_id or job_id in seen:
                    continue
                seen.add(job_id)

                pub = str(item.get("pubDate") or "")
                if pub > highest:
                    highest = pub
                if watermark and pub and pub <= watermark:
                    continue

                job = self._to_job(item)
                if job:
                    count += 1
                    yield job
            log.info("jobicy 切片 %-16s 新增 %d 条", label, count)

        self.next_cursor = {"max_pub_date": highest}

    def _to_job(self, item: dict) -> Job | None:
        title = (item.get("jobTitle") or "").strip()
        if not title:
            return None

        job_type = item.get("jobType")
        if isinstance(job_type, list):
            job_type = job_type[0] if job_type else None

        geo = item.get("jobGeo") or ""
        industry = item.get("jobIndustry")
        tags = industry if isinstance(industry, list) else ([industry] if industry else [])

        level = item.get("jobLevel")
        seniority = level if isinstance(level, list) else ([level] if level else [])

        return Job(
            source=self.name,
            source_id=str(item["id"]),
            url=item.get("url"),
            title=title,
            description=item.get("jobDescription") or item.get("jobExcerpt"),
            company_name=(item.get("companyName") or "").strip() or None,
            company_logo=item.get("companyLogo") or None,
            employment_type=_EMPLOYMENT.get(str(job_type or "").strip().lower(), job_type),
            remote_type="remote",  # 站点只收远程岗
            seniority=[str(s) for s in seniority if s],
            locations=[p.strip() for p in str(geo).split(",") if p.strip()],
            tags=[str(t) for t in tags if t],
            # 薪资字段只在部分记录上出现（小样本探测时可能整批为空，容易误判为「接口不提供」）
            salary_min=item.get("salaryMin"),
            salary_max=item.get("salaryMax"),
            salary_currency=item.get("salaryCurrency"),
            salary_period=item.get("salaryPeriod"),  # 'yearly' 由 models 归一为 annual
            posted_at=self._date(item.get("pubDate")),
            raw=item,
        )

    @staticmethod
    def _date(value) -> datetime | None:
        if not value:
            return None
        text = str(value).strip().replace("Z", "+00:00")
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d"):
            try:
                return datetime.strptime(text, fmt)
            except ValueError:
                continue
        try:
            return datetime.fromisoformat(text)
        except ValueError:
            return None
