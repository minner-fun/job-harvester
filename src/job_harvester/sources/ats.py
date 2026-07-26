"""ATS — 直接读公司自己的招聘看板。

和其他源的区别：这不是"爬一个站"，而是按 `config/ats-boards.toml` 里的公司清单，
逐家调它所在 ATS 的公开 API。一家公司一次请求。

    Ashby       GET https://api.ashbyhq.com/posting-api/job-board/{board}
    Lever       GET https://api.lever.co/v0/postings/{board}?mode=json
    Greenhouse  GET https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true

三家都免费公开、无需认证。

为什么要单独做这个源：
    聚合站是二手数据，有损。实测 TRM Labs 在聚合站里只能查到 3 个岗位，
    而它 Ashby 看板上有 122 个；Provable、Goldsky 在聚合站里则完全查不到。
    库里已有 655 个岗位的申请链接指向这三家 ATS —— 说明聚合站本身也是从
    这些看板抓的，我们等于在拿三手数据。

增量：这些看板体量小（一家几个到上百个岗位），每轮全取再靠 content_hash
      判断变更即可，不需要水位线。下架的岗位会停止出现，可用 last_seen_at 识别。
"""

from __future__ import annotations

import html
import logging
import os
import tomllib
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path

from selectolax.parser import HTMLParser

from ..models import Job, ts_from_millis
from .base import Source

log = logging.getLogger(__name__)

# 看板清单是**用户数据**，不是包的一部分。按 ATS_BOARDS_CONFIG 环境变量定位，
# 默认取工作目录下的 config/ats-boards.toml。
# 不能用「相对包安装位置」的路径 —— 本项目作为依赖被安装后那个路径会指到
# site-packages 里去。
DEFAULT_CONFIG = Path("config/ats-boards.toml")


def config_path() -> Path:
    return Path(os.environ.get("ATS_BOARDS_CONFIG") or DEFAULT_CONFIG)

ASHBY = "https://api.ashbyhq.com/posting-api/job-board/{}"
LEVER = "https://api.lever.co/v0/postings/{}"
GREENHOUSE = "https://boards-api.greenhouse.io/v1/boards/{}/jobs"

_EMPLOYMENT = {
    "fulltime": "full-time", "full-time": "full-time",
    "parttime": "part-time", "part-time": "part-time",
    "intern": "internship", "internship": "internship",
    "contract": "contract", "contractor": "contract",
    "temporary": "temporary",
}
_WORKPLACE = {
    "remote": "remote",
    "hybrid": "hybrid",
    "onsite": "onsite", "on-site": "onsite",
}


def _text(value) -> str:
    """把可能带 HTML（甚至 HTML 实体转义过的）的字段转成纯文本。"""
    if not value:
        return ""
    s = str(value)
    if "&lt;" in s or "&amp;" in s:
        s = html.unescape(s)
    if "<" in s:
        s = HTMLParser(s).text(separator="\n", strip=True)
    return s.strip()


class AtsSource(Source):
    name = "ats"
    delay = 1.0
    # 每轮把每个看板的全部岗位取一遍，故可据「本轮未见到」判定下架
    enumerates_all = True

    def fetch(self, *, full: bool = False) -> AsyncIterator[Job]:
        return self._iter(full=full)

    async def _iter(self, *, full: bool) -> AsyncIterator[Job]:
        boards = self._load_boards()
        if not boards:
            log.error("未读到任何看板配置: %s", config_path())
            return
        log.info("ats 共 %d 个看板", len(boards))

        handlers = {
            "ashby": self._ashby,
            "lever": self._lever,
            "greenhouse": self._greenhouse,
        }

        for entry in boards:
            ats = str(entry.get("ats", "")).lower()
            board = entry.get("board")
            company = entry.get("company") or board
            handler = handlers.get(ats)
            if not handler or not board:
                log.warning("跳过无效配置: %s", entry)
                continue

            try:
                jobs = await handler(board, company)
            except Exception as exc:  # noqa: BLE001 — 单个看板失败不该中断整轮
                log.warning("ats %s/%s 拉取失败: %s", ats, board, exc)
                continue

            log.info("ats %-11s %-22s %d 个岗位", ats, board, len(jobs))
            for job in jobs:
                yield job

        self.next_cursor = {"boards": len(boards)}

    @staticmethod
    def _load_boards() -> list[dict]:
        path = config_path()
        if not path.exists():
            log.error(
                "找不到看板配置 %s。复制 config/ats-boards.example.toml 过去，"
                "或用 ATS_BOARDS_CONFIG 指定路径。", path,
            )
            return []
        with path.open("rb") as fh:
            return tomllib.load(fh).get("boards", [])

    # ---------------------------------------------------------------- Ashby
    async def _ashby(self, board: str, company: str) -> list[Job]:
        payload = await self.fetcher.get_json(
            ASHBY.format(board), params={"includeCompensation": "true"}
        )
        out = []
        for item in (payload or {}).get("jobs", []):
            # isListed=False 是已下架但接口仍返回的岗位
            if item.get("isListed") is False:
                continue
            job_id = item.get("id")
            title = (item.get("title") or "").strip()
            if not job_id or not title:
                continue

            # secondaryLocations 的元素是 {location, address:{...}} 对象而非字符串，
            # 直接 str() 会把整个字典塞进地点字段
            locations = [item.get("location")]
            for sec in item.get("secondaryLocations") or []:
                locations.append(sec.get("location") if isinstance(sec, dict) else sec)
            workplace = str(item.get("workplaceType") or "").lower().replace(" ", "")
            remote = _WORKPLACE.get(workplace)
            if remote is None and item.get("isRemote"):
                remote = "remote"

            smin, smax, currency, period = self._ashby_salary(item.get("compensation"))

            out.append(Job(
                source=self.name,
                source_id=f"ashby:{board}:{job_id}",
                url=item.get("jobUrl") or item.get("applyUrl"),
                title=title,
                description=_text(item.get("descriptionPlain") or item.get("descriptionHtml")),
                company_name=company,
                employment_type=_EMPLOYMENT.get(
                    str(item.get("employmentType") or "").lower()
                ),
                remote_type=remote,
                locations=[str(x) for x in locations if x],
                tags=[t for t in (item.get("department"), item.get("team")) if t],
                salary_min=smin,
                salary_max=smax,
                salary_currency=currency,
                salary_period=period,
                posted_at=self._iso(item.get("publishedAt")),
                raw={**item, "_ats": "ashby", "_board": board},
            ))
        return out

    @staticmethod
    def _ashby_salary(node) -> tuple[float | None, float | None, str | None, str | None]:
        """Ashby 的 compensation 结构层级较深且经常整块为空。"""
        if not isinstance(node, dict):
            return None, None, None, None
        tiers = node.get("compensationTiers") or []
        for tier in tiers:
            if not isinstance(tier, dict):
                continue
            for comp in tier.get("components") or []:
                if not isinstance(comp, dict):
                    continue
                if str(comp.get("compensationType") or "").lower() != "salary":
                    continue
                try:
                    smin = float(comp["minValue"]) if comp.get("minValue") else None
                    smax = float(comp["maxValue"]) if comp.get("maxValue") else smin
                except (TypeError, ValueError):
                    continue
                if smin:
                    interval = str(comp.get("interval") or "").upper()
                    period = {"1 YEAR": "annual", "1 MONTH": "monthly",
                              "1 HOUR": "hourly"}.get(interval)
                    return smin, smax, comp.get("currencyCode"), period
        return None, None, None, None

    # ---------------------------------------------------------------- Lever
    async def _lever(self, board: str, company: str) -> list[Job]:
        payload = await self.fetcher.get_json(LEVER.format(board), params={"mode": "json"})
        out = []
        for item in payload or []:
            job_id = item.get("id")
            title = (item.get("text") or "").strip()
            if not job_id or not title:
                continue

            cats = item.get("categories") or {}
            desc = "\n\n".join(
                p for p in (item.get("descriptionPlain"), item.get("additionalPlain")) if p
            )
            smin, smax, currency, period = self._lever_salary(item.get("salaryRange"))

            out.append(Job(
                source=self.name,
                source_id=f"lever:{board}:{job_id}",
                url=item.get("hostedUrl") or item.get("applyUrl"),
                title=title,
                description=_text(desc),
                company_name=company,
                employment_type=_EMPLOYMENT.get(str(cats.get("commitment") or "").lower()),
                remote_type=_WORKPLACE.get(str(item.get("workplaceType") or "").lower()),
                locations=[x for x in (cats.get("location"), item.get("country")) if x],
                tags=[x for x in (cats.get("department"), cats.get("team")) if x],
                salary_min=smin,
                salary_max=smax,
                salary_currency=currency,
                salary_period=period,
                posted_at=ts_from_millis(item.get("createdAt")),
                raw={**item, "_ats": "lever", "_board": board},
            ))
        return out

    @staticmethod
    def _lever_salary(node) -> tuple[float | None, float | None, str | None, str | None]:
        if not isinstance(node, dict):
            return None, None, None, None
        try:
            smin = float(node["min"]) if node.get("min") else None
            smax = float(node["max"]) if node.get("max") else smin
        except (TypeError, ValueError):
            return None, None, None, None
        period = {"per-year-salary": "annual", "per-hour-wage": "hourly",
                  "per-month-salary": "monthly"}.get(str(node.get("interval") or "").lower())
        return smin, smax, node.get("currency"), period

    # ----------------------------------------------------------- Greenhouse
    async def _greenhouse(self, board: str, company: str) -> list[Job]:
        payload = await self.fetcher.get_json(
            GREENHOUSE.format(board), params={"content": "true"}
        )
        out = []
        for item in (payload or {}).get("jobs", []):
            job_id = item.get("id")
            title = (item.get("title") or "").strip()
            if not job_id or not title:
                continue

            loc = item.get("location") or {}
            loc_name = loc.get("name") if isinstance(loc, dict) else str(loc or "")
            offices = [o.get("name") for o in (item.get("offices") or [])
                       if isinstance(o, dict) and o.get("name")]
            departments = [d.get("name") for d in (item.get("departments") or [])
                           if isinstance(d, dict) and d.get("name")]

            # Greenhouse 不给结构化的远程标记，只能从地点文本判断
            blob = f"{loc_name} {' '.join(offices)}".lower()
            remote = "remote" if "remote" in blob else None

            out.append(Job(
                source=self.name,
                source_id=f"greenhouse:{board}:{job_id}",
                url=item.get("absolute_url"),
                # content 是 HTML 实体转义过的 HTML，要先 unescape 再去标签
                description=_text(item.get("content")),
                title=title,
                company_name=item.get("company_name") or company,
                remote_type=remote,
                locations=[x for x in ([loc_name] + offices) if x],
                tags=departments,
                posted_at=self._iso(item.get("first_published") or item.get("updated_at")),
                raw={**item, "_ats": "greenhouse", "_board": board},
            ))
        return out

    @staticmethod
    def _iso(value) -> datetime | None:
        if not value:
            return None
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
