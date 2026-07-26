"""dejob.ai — 中文 Web3 / 远程岗位站。

接口从站点 SPA bundle (/static/js/main.<hash>.js) 中提取：
    GET https://dejob.ai/api/worker/topics?page=N&limit=100

注意该站术语与直觉相反：`worker` 是岗位，`employ` 是简历。
robots.txt 为 `User-agent: * / Disallow:`（空值 = 完全允许）。
"""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator

from ..models import Job, ts_from_millis
from .base import Source

log = logging.getLogger(__name__)

API = "https://dejob.ai/api/worker/topics"
DETAIL = "https://dejob.ai/jobDetail?id={}"
PAGE_LIMIT = 100  # 实测生效，total 4658 → 47 页全量

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$", re.I)

# officeModeName -> remote_type
_OFFICE_MODE = {
    "Remote": "remote",
    "On-site": "onsite",
    "Remote/On-site": "hybrid",
}

# workTypeName -> employment_type
_WORK_TYPE = {
    "Full Time": "full-time",
    "Part Time": "part-time",
    "Full/Part": "full-time",
    "Intern": "internship",
}


class DejobSource(Source):
    name = "dejob"
    delay = 1.0

    def fetch(self, *, full: bool = False) -> AsyncIterator[Job]:
        return self._iter(full=full)

    async def _iter(self, *, full: bool) -> AsyncIterator[Job]:
        # 水位线：上次见过的最大 createTime（毫秒）
        watermark = 0 if full else int(self.cursor.get("max_created_ms") or 0)
        highest = watermark
        page = 1

        while True:
            payload = await self.fetcher.get_json(
                API, params={"page": page, "limit": PAGE_LIMIT}
            )
            if not isinstance(payload, dict) or payload.get("errorCode") not in (0, None):
                log.warning("dejob 返回异常: %s", str(payload)[:200])
                break

            data = payload.get("data") or {}
            results = data.get("results")
            # page 超出末页时 results 为 null，作为结束标志
            if not results:
                break

            total = (data.get("page") or {}).get("total")
            log.info("dejob page=%s 取回 %d 条 (total=%s)", page, len(results), total)

            stop = False
            for item in results:
                created = int(item.get("createTime") or 0)
                highest = max(highest, created)
                # 列表按 createTime 倒序，遇到不新于水位线的即可停
                if watermark and created and created <= watermark:
                    stop = True
                    break
                job = self._to_job(item)
                if job:
                    yield job

            if stop:
                log.info("dejob 触及水位线，提前结束于 page=%s", page)
                break
            if len(results) < PAGE_LIMIT:
                break
            page += 1

        self.next_cursor = {"max_created_ms": highest}

    # ------------------------------------------------------------------ 映射
    def _to_job(self, item: dict) -> Job | None:
        topic_id = item.get("topicId")
        if not topic_id:
            return None

        title = (item.get("positionName") or "").strip()
        if not title:
            return None

        smin, smax = self._salary(item)

        return Job(
            source=self.name,
            source_id=str(topic_id),
            url=item.get("url") or DETAIL.format(topic_id),
            title=title,
            description=self._description(item),
            company_name=(item.get("company") or "").strip() or None,
            company_website=item.get("companyWebsite") or None,
            company_logo=item.get("companyLogo") or None,
            employment_type=_WORK_TYPE.get(item.get("workTypeName")),
            remote_type=_OFFICE_MODE.get(item.get("officeModeName")),
            # leverName 是 Urgent/Normal 紧急度标记，不是职级，不映射到 seniority
            seniority=[],
            locations=self._locations(item),
            tags=[t.get("tagName") for t in (item.get("tags") or []) if t.get("tagName")],
            salary_min=smin,
            salary_max=smax,
            # 币种与周期由站点渲染代码坐实（bundle 内 `${Nb(minSalary)} - ${Nb(maxSalary)} / month`，
            # Nb = Intl.NumberFormat(currency:"USD")），接口本身不返回这两个字段。
            salary_currency="USD" if (smin or smax) else None,
            salary_period="monthly" if (smin or smax) else None,
            posted_at=ts_from_millis(item.get("createTime")),
            raw=item,
            contact=self._contact(item),
        )

    # 月薪 USD 的合理区间。发布者常用 min=1 / max=99999999 表示「面议」，
    # 不过滤会让薪资统计出现 1 美元和 1 亿美元的月薪。
    SALARY_FLOOR = 100
    SALARY_CEIL = 100_000
    # 真实薪资区间很少超过 4 倍；10-1000000 这种是占位而非区间。
    SALARY_MAX_RATIO = 20

    @classmethod
    def _salary(cls, item: dict) -> tuple[float | None, float | None]:
        def sane(value) -> float | None:
            try:
                num = float(value)
            except (TypeError, ValueError):
                return None
            if num < cls.SALARY_FLOOR or num > cls.SALARY_CEIL:
                return None
            return num

        smin, smax = sane(item.get("minSalary")), sane(item.get("maxSalary"))
        # 只剩一端时区间无意义，一并丢弃
        if smin is None or smax is None:
            return None, None
        if smin > smax:
            return None, None
        if smin and smax / smin > cls.SALARY_MAX_RATIO:
            return None, None
        return smin, smax

    @staticmethod
    def _description(item: dict) -> str | None:
        """content=岗位职责, content2=任职要求, content3=待遇补充。"""
        parts = [
            item.get("companyIntroduction"),
            item.get("content"),
            item.get("content2"),
            item.get("content3"),
            item.get("content5"),
        ]
        text = "\n\n".join(p.strip() for p in parts if p and str(p).strip())
        return text or None

    @staticmethod
    def _locations(item: dict) -> list[str]:
        """该站 `location` 恒为空，实际地点写在 `base` 里。"""
        raw = item.get("base") or item.get("location") or ""
        if not raw:
            return []
        return [p.strip() for p in re.split(r"[/、,，|]", str(raw)) if p.strip()]

    @staticmethod
    def _contact(item: dict) -> dict | None:
        """联系方式隔离存储。

        实测列表接口的 email/phone/telegram/wechat 均为空，
        真实 PII 在 user 对象：nickname 与 walletAddress 约 76% 是邮箱、
        24% 是 0x 钱包地址。
        """
        user = item.get("user") or {}
        nickname = (user.get("nickname") or "").strip()
        wallet = (user.get("walletAddress") or "").strip()

        email = item.get("email") or None
        if not email and _EMAIL.match(nickname):
            email = nickname
        if not email and _EMAIL.match(wallet):
            email = wallet

        contact = {
            "email": email,
            "phone": item.get("phone") or None,
            "telegram": item.get("telegram") or None,
            "wechat": item.get("wechat") or None,
            "wallet_address": wallet if wallet.startswith("0x") else None,
        }
        return contact if any(contact.values()) else None
