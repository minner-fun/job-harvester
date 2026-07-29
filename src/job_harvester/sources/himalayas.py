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

# 全量续跑时往回退的 offset 数。
#
# 列表按 pubDate 倒序，新岗位插在最前面，所以随着时间推移，原先在 offset X
# 的那条会漂到 X+N —— 从 X 续跑只会重复处理已经见过的（upsert 判为无变化，
# 无害），不会漏。真正会漏的是反方向：过期岗位被摘掉时整体前移。
# 退这么一段把摘除量盖住；代价只有约 23 次请求。
RESUME_REWIND = 200
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
    # 全量要打一万多次请求。实测按 1 次/秒 压了 45 分钟后站点开始返回 500，
    # 响应也从 1 秒退化到 28 秒，重试预算耗尽后整轮中止。放慢并更耐心一些。
    full_delay = 2.5
    full_max_retries = 8

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._warned_placeholder = False

    def fetch(self, *, full: bool = False) -> AsyncIterator[Job]:
        return self._iter(full=full)

    async def _iter(self, *, full: bool) -> AsyncIterator[Job]:
        stored = int(self.cursor.get("max_pub_date") or 0)
        # 停止条件：全量不看水位线，从头走到尾
        watermark = 0 if full else stored
        # 但 highest 无论如何都要从**已有水位线**起步，否则水位线会倒退：
        # 续跑是从中途某个 offset 开始的，它前面那些更新的条目这一轮根本没看到，
        # 从 0 起算得到的最大 pubDate 会比原水位线还旧。实测一次续跑就把水位线
        # 从 07-28 推回了 07-23 —— 不丢数据（下一轮增量会重抓），但白跑一大段。
        highest = stored
        total: int | None = None

        # 上一次全量走到哪了。只有全量用得上：增量本来就是从头看几页就停。
        pending = int(self.cursor.get("full_offset") or 0)
        offset = max(0, pending - RESUME_REWIND) if (full and pending) else 0
        if offset:
            log.info("himalayas 全量从 offset=%s 续跑（上次停在 %s）", offset, pending)
            self.resume_state = {"full_offset": offset}

        walked_to_end = False
        while True:
            payload = await self.fetcher.get_json(
                API, params={"limit": PAGE_LIMIT, "offset": offset}
            )
            if not isinstance(payload, dict):
                log.warning("himalayas 返回非预期结构，终止")
                break

            jobs = payload.get("jobs") or []
            if not jobs:
                walked_to_end = True
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
            # 每翻完一页就记一次进度：这一页往后要是挂了，下次从这里接着走
            if full:
                self.resume_state = {"full_offset": offset}
            if total and offset >= total:
                walked_to_end = True
                break
            if offset and offset % (PAGE_LIMIT * 50) == 0:
                log.info("himalayas 进度 offset=%s / %s", offset, total)

        self.next_cursor = {"max_pub_date": highest}

        # 续跑进度的去留，三种情况各不相同：
        if full and walked_to_end:
            # 真的走到底了，清掉，下次 --full 从头来
            self.resume_state = {}
            log.info("himalayas 全量走完 offset=%s / %s", offset, total)
        elif full:
            # 提前退出（结构异常等）：进度留着，下次接着走
            self.next_cursor["full_offset"] = offset
        elif self.cursor.get("full_offset"):
            # 增量必须**原样带上**别人的进度 —— 否则两次全量之间的任何一轮
            # cron 增量都会把 full_offset 抹掉，续跑就永远等不到
            self.next_cursor["full_offset"] = self.cursor["full_offset"]

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
