"""CryptoJobsList — Web3 岗位站，约 440 个在架岗位。

策略：sitemap-jobs.xml 拿 URL + lastmod，再抓岗位页解析 `__NEXT_DATA__`。

合规：robots.txt 对 `*` 明确 `Disallow: /api/`，但 `/jobs/` 是 `Allow`，
      因此只走岗位页，不碰内部 API。

数据来源选择：
    岗位页同时有 JSON-LD 和 `__NEXT_DATA__`。后者是 72 字段的完整 job 对象，
    且带 JSON-LD 没有的 Web3 维度（coinSymbol / companyVerified / paysInCrypto /
    visaSponsor / paidRelocation），所以以 `__NEXT_DATA__` 为准。
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import AsyncIterator
from datetime import datetime

from ..models import Job
from .base import Source

log = logging.getLogger(__name__)

SITEMAP = "https://cryptojobslist.com/sitemap-jobs.xml"
NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)

_EMPLOYMENT = {
    "FULL_TIME": "full-time",
    "PART_TIME": "part-time",
    "CONTRACTOR": "contract",
    "CONTRACT": "contract",
    "INTERN": "internship",
    "TEMPORARY": "temporary",
}
_PERIOD = {"HOUR": "hourly", "DAY": "daily", "WEEK": "weekly", "MONTH": "monthly", "YEAR": "annual"}

# 招聘人姓名/头像，不入 raw
_DROP_FIELDS = ("bossFirstName", "bossLastName", "bossPicture")


class CryptoJobsListSource(Source):
    name = "cryptojobslist"
    delay = 1.0
    needs_known_marks = True
    known_mark_by = "_url"

    def fetch(self, *, full: bool = False) -> AsyncIterator[Job]:
        return self._iter(full=full)

    async def _iter(self, *, full: bool) -> AsyncIterator[Job]:
        body = (await self.fetcher.get(SITEMAP)).text
        entries: list[tuple[str, str]] = []
        for block in re.findall(r"<url>(.*?)</url>", body, re.S):
            loc = re.search(r"<loc>([^<]+)</loc>", block)
            if not loc or "/jobs/" not in loc.group(1):
                continue
            lastmod = re.search(r"<lastmod>([^<]+)</lastmod>", block)
            entries.append((loc.group(1).strip(), lastmod.group(1).strip() if lastmod else ""))
        entries.sort(key=lambda x: x[1], reverse=True)
        log.info("cryptojobslist sitemap 共 %d 个岗位页", len(entries))

        watermark = "" if full else str(self.cursor.get("max_lastmod") or "")
        known = self.known_marks or {}

        highest = watermark
        for url, lastmod in entries:
            if watermark and lastmod and lastmod <= watermark:
                continue
            if lastmod and known.get(url) == lastmod:
                continue
            try:
                html = (await self.fetcher.get(url)).text
            except Exception as exc:  # noqa: BLE001 — 单页失败不该中断整轮
                log.warning("cryptojobslist 抓取失败 %s: %s", url, exc)
                continue

            job = self._parse(html, url, lastmod)
            if job:
                yield job
            if lastmod and lastmod > highest:
                highest = lastmod

        self.next_cursor = {"max_lastmod": highest}

    def _parse(self, html: str, url: str, lastmod: str) -> Job | None:
        m = NEXT_DATA_RE.search(html)
        if not m:
            log.debug("cryptojobslist 无 __NEXT_DATA__: %s", url)
            return None
        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError:
            log.warning("cryptojobslist __NEXT_DATA__ 解析失败: %s", url)
            return None

        item = (data.get("props") or {}).get("pageProps", {}).get("job")
        if not isinstance(item, dict):
            return None

        title = (item.get("jobTitle") or "").strip()
        job_id = item.get("id")
        if not title or not job_id:
            return None

        emp = item.get("employmentType")
        if isinstance(emp, list):
            emp = emp[0] if emp else None

        smin, smax, currency, period = self._salary(item.get("salary"))

        raw = {k: v for k, v in item.items() if k not in _DROP_FIELDS}
        raw["_lastmod"] = lastmod
        raw["_url"] = url

        location = (item.get("jobLocation") or "").strip()
        return Job(
            source=self.name,
            source_id=str(job_id),
            url=url,
            title=title,
            description=item.get("jobDescription"),
            company_name=(item.get("companyName") or "").strip() or None,
            company_website=item.get("companyUrl") or None,
            company_logo=item.get("companyLogo") or None,
            employment_type=_EMPLOYMENT.get(str(emp or "").upper(), emp),
            remote_type="remote" if item.get("remote") else None,
            locations=[p.strip() for p in location.split(",") if p.strip()],
            tags=[str(t) for t in (item.get("tags") or []) if t],
            salary_min=smin,
            salary_max=smax,
            salary_currency=currency,
            salary_period=period,
            posted_at=self._date(item.get("publishedAt") or item.get("createdAt")),
            raw=raw,
        )

    @staticmethod
    def _salary(node) -> tuple[float | None, float | None, str | None, str | None]:
        if not isinstance(node, dict):
            return None, None, None, None

        def num(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        smin = num(node.get("minValue")) or num(node.get("value"))
        smax = num(node.get("maxValue")) or smin
        period = _PERIOD.get(str(node.get("unitText") or "").upper())
        return smin, smax, node.get("currency"), period

    @staticmethod
    def _date(value) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
