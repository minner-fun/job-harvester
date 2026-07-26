"""web3.career — Web3 岗位站，约 27,000 个岗位页。

策略：sitemap 拿 URL + lastmod，再逐个抓详情页解析 JSON-LD。

为什么不走列表页（`/?page=N`）：
    列表页确实带 JSON-LD，但**页面上的 JSON-LD 块与表格行不是同一批岗位**
    （实测 page=2 有 15 行 / 20 个 JSON-LD，按 slug 只能对上 10 个；
    首页 20 行 20 块，按顺序配对 0/20 全错位），多出的是侧边推广位。
    因此无法把 JSON-LD 的详细数据可靠地关联到具体岗位 URL。

为什么不走官方 API：
    `https://web3.career/api/v1` 需要注册 token，且文档明确
    `Pagination: Via limit parameter (no cursor/offset)`，单次最多 100 条且翻不了历史。

详情页解析注意：
    一个详情页含 **2 个 JobPosting 块** —— 第一个是本岗位，第二个是页面下方的推荐岗位
    （实测抓 Product Owner 页时第二块是完全无关的 moomoo 岗位）。
    必须按 URL slug 校验，不能盲取第一个。
    另有 JSON-LD 块会让 json.loads 抛 `Extra data`，需逐块容错。
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import AsyncIterator
from datetime import datetime

from selectolax.parser import HTMLParser

from ..models import Job
from .base import Source

log = logging.getLogger(__name__)

SITEMAP_INDEX = "https://web3.career/sitemap.xml"
JOB_URL_RE = re.compile(r"^https://web3\.career/(?P<slug>[^/]+)/(?P<id>\d+)$")
LD_RE = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.S | re.I
)

_EMPLOYMENT = {
    "full-time": "full-time",
    "part-time": "part-time",
    "contractor": "contract",
    "contract": "contract",
    "intern": "internship",
    "internship": "internship",
    "temporary": "temporary",
}

_PERIOD = {"HOUR": "hourly", "DAY": "daily", "WEEK": "weekly", "MONTH": "monthly", "YEAR": "annual"}


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")


class Web3CareerSource(Source):
    name = "web3career"
    delay = 1.0
    needs_known_marks = True

    def fetch(self, *, full: bool = False) -> AsyncIterator[Job]:
        return self._iter(full=full)

    async def _iter(self, *, full: bool) -> AsyncIterator[Job]:
        entries = await self._sitemap_entries()
        log.info("web3career sitemap 共 %d 个岗位页", len(entries))

        watermark = "" if full else str(self.cursor.get("max_lastmod") or "")
        # 已入库且 lastmod 未变的直接跳过 —— 首次全量耗时数小时，必须能中断续跑
        known = self.known_marks or {}

        todo = []
        for url, lastmod, job_id in entries:
            if watermark and lastmod and lastmod <= watermark:
                continue
            if known.get(job_id) == lastmod and lastmod:
                continue
            todo.append((url, lastmod, job_id))

        skipped = len(entries) - len(todo)
        log.info("web3career 待抓 %d 个（跳过已抓且未变更 %d 个）", len(todo), skipped)

        highest = watermark
        done = 0
        for url, lastmod, job_id in todo:
            try:
                resp = await self.fetcher.get(url)
            except Exception as exc:  # noqa: BLE001 — 单页失败不该中断整轮
                log.warning("web3career 抓取失败 %s: %s", url, exc)
                continue

            job = self._parse_detail(resp.text, url, job_id, lastmod)
            if job:
                yield job
            if lastmod and lastmod > highest:
                highest = lastmod

            done += 1
            if done % 200 == 0:
                log.info("web3career 进度 %d / %d", done, len(todo))

        self.next_cursor = {"max_lastmod": highest}

    # -------------------------------------------------------------- sitemap
    async def _sitemap_entries(self) -> list[tuple[str, str, str]]:
        index = (await self.fetcher.get(SITEMAP_INDEX)).text
        maps = re.findall(r"<loc>([^<]+)</loc>", index)

        seen: dict[str, tuple[str, str, str]] = {}
        for sm in maps:
            try:
                body = (await self.fetcher.get(sm)).text
            except Exception as exc:  # noqa: BLE001
                log.warning("web3career sitemap 读取失败 %s: %s", sm, exc)
                continue
            for block in re.findall(r"<url>(.*?)</url>", body, re.S):
                loc = re.search(r"<loc>([^<]+)</loc>", block)
                if not loc:
                    continue
                m = JOB_URL_RE.match(loc.group(1).strip())
                if not m:
                    continue  # sitemap1/3 大量是 /xxx-jobs 标签聚合页，不是岗位
                lastmod = re.search(r"<lastmod>([^<]+)</lastmod>", block)
                seen[m.group("id")] = (
                    loc.group(1).strip(),
                    (lastmod.group(1).strip() if lastmod else ""),
                    m.group("id"),
                )
        # 新的在前，便于中断后优先补齐最新数据
        return sorted(seen.values(), key=lambda x: x[1], reverse=True)

    # ---------------------------------------------------------------- 解析
    def _parse_detail(self, html: str, url: str, job_id: str, lastmod: str) -> Job | None:
        postings = []
        for raw_block in LD_RE.findall(html):
            try:
                data = json.loads(raw_block.strip())
            except json.JSONDecodeError:
                continue  # 站点存在畸形 JSON-LD 块，跳过而非中断整页
            for item in data if isinstance(data, list) else [data]:
                if not isinstance(item, dict):
                    continue
                if item.get("@type") == "JobPosting":
                    postings.append(item)
                for sub in item.get("@graph", []) or []:
                    if isinstance(sub, dict) and sub.get("@type") == "JobPosting":
                        postings.append(sub)

        if not postings:
            return None

        item = self._pick_posting(postings, url)
        if item is None:
            return None

        title = self._text(item.get("title"))
        if not title:
            return None

        org = item.get("hiringOrganization") or {}
        company = org.get("name") if isinstance(org, dict) else org
        smin, smax, currency, period = self._salary(item.get("baseSalary"))

        raw = dict(item)
        raw["_lastmod"] = lastmod
        raw["_url"] = url

        return Job(
            source=self.name,
            source_id=job_id,
            url=url,
            title=title,
            description=self._text(item.get("description")),
            company_name=self._text(company) or None,
            employment_type=_EMPLOYMENT.get(
                str(item.get("employmentType") or "").strip().lower(),
                self._text(item.get("employmentType")) or None,
            ),
            remote_type="remote" if item.get("jobLocationType") == "TELECOMMUTE" else None,
            locations=self._locations(item),
            tags=[t for t in [self._text(item.get("occupationalCategory")),
                              self._text(item.get("industry"))] if t],
            salary_min=smin,
            salary_max=smax,
            salary_currency=currency,
            salary_period=period,
            posted_at=self._date(item.get("datePosted")),
            expires_at=self._date(item.get("validThrough")),
            raw=raw,
        )

    @staticmethod
    def _pick_posting(postings: list[dict], url: str) -> dict | None:
        """详情页有多个 JobPosting，按 URL slug 选出属于本页的那个。"""
        m = JOB_URL_RE.match(url)
        if not m:
            return postings[0]
        want = m.group("slug")

        for item in postings:
            org = item.get("hiringOrganization") or {}
            name = org.get("name") if isinstance(org, dict) else org
            got = slugify(f"{item.get('title')} {name}")
            if got and (want.startswith(got) or got.startswith(want)):
                return item
        # 匹配不上时退回第一个（推荐位通常排在后面），但记下来便于排查
        log.debug("web3career slug 未匹配，退回首个 JobPosting: %s", url)
        return postings[0]

    @staticmethod
    def _text(value) -> str:
        if value is None:
            return ""
        if isinstance(value, dict):
            value = value.get("name") or value.get("@value") or ""
        text = str(value)
        if "<" in text:
            text = HTMLParser(text).text(separator="\n", strip=True)
        return re.sub(r"&amp;", "&", text).strip()

    @classmethod
    def _locations(cls, item: dict) -> list[str]:
        out: list[str] = []
        loc = item.get("jobLocation")
        for entry in loc if isinstance(loc, list) else [loc]:
            if not isinstance(entry, dict):
                continue
            addr = entry.get("address") or {}
            if isinstance(addr, dict):
                parts = [
                    addr.get("addressLocality"),
                    addr.get("addressRegion"),
                    addr.get("addressCountry"),
                ]
                joined = ", ".join(p for p in parts if p and str(p).strip())
                if joined:
                    out.append(joined)
        req = item.get("applicantLocationRequirements")
        for entry in req if isinstance(req, list) else [req]:
            if isinstance(entry, dict) and entry.get("name"):
                out.append(str(entry["name"]))
        return out

    @staticmethod
    def _salary(node) -> tuple[float | None, float | None, str | None, str | None]:
        if not isinstance(node, dict):
            return None, None, None, None
        currency = node.get("currency") or node.get("currencyCode")
        value = node.get("value")
        if not isinstance(value, dict):
            return None, None, currency, None

        def num(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        smin = num(value.get("minValue")) or num(value.get("value"))
        smax = num(value.get("maxValue")) or smin
        period = _PERIOD.get(str(value.get("unitText") or "").upper())
        return smin, smax, currency, period

    @staticmethod
    def _date(value) -> datetime | None:
        if not value:
            return None
        text = str(value).strip()
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S %z", "%Y-%m-%d"):
            try:
                return datetime.strptime(text, fmt)
            except ValueError:
                continue
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
