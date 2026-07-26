"""Cryptocurrency Jobs — Web3/加密垂直招聘站。

    GET https://cryptocurrencyjobs.co/index.xml     # RSS，75 条最新在招岗位

为什么走 RSS 而不是 sitemap：
    `sitemap-jobs.xml` 里有 19,999 个岗位 URL，但**没有 lastmod**，
    而且岗位页**既没有 JSON-LD 也没有 __NEXT_DATA__** —— 意味着两万次请求
    加上脆弱的 HTML 选择器解析，且其中大部分是早已过期、仅仅还留着页面的死岗。
    RSS 一次请求就能拿到 75 条真正在招的，成本差了四个数量级。

    分类 feed（/engineering/index.xml 等）实测 11 个全部 404，只有主 feed。

局限：只有最新 75 条，没有历史。所以要靠定时轮询持续跟，漏采补不回来
      —— 和 RemoteOK 是同一类滚动窗口源。

robots：`User-agent: *` 下是 `Disallow: /*?`，只禁带查询串的 URL；
        `/index.xml` 与岗位页都是静态路径，不受限。
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from collections.abc import AsyncIterator
from datetime import datetime
from email.utils import parsedate_to_datetime

from ..models import Job
from .base import Source

log = logging.getLogger(__name__)

FEED = "https://cryptocurrencyjobs.co/index.xml"

# 标题格式固定为 "岗位名 at 公司名"，公司名里可能含 " at " 之外的分隔符
# （例如 "Software Engineer (Indexer Focus) - Shielded at Input | Output"），
# 所以从右侧切最后一个 " at "。
TITLE_RE = re.compile(r"^(?P<title>.+?)\s+at\s+(?P<company>[^,]+)$")

# URL 形如 /{分类}/{公司-岗位-slug}/
URL_RE = re.compile(r"^https://cryptocurrencyjobs\.co/(?P<category>[^/]+)/(?P<slug>[^/]+)/?$")


class CryptocurrencyJobsSource(Source):
    name = "cryptocurrencyjobs"
    delay = 1.0

    def fetch(self, *, full: bool = False) -> AsyncIterator[Job]:
        return self._iter(full=full)

    async def _iter(self, *, full: bool) -> AsyncIterator[Job]:
        watermark = "" if full else str(self.cursor.get("max_pub_date") or "")
        highest = watermark

        body = (await self.fetcher.get(FEED)).text
        try:
            root = ET.fromstring(body)
        except ET.ParseError as exc:
            log.error("cryptocurrencyjobs RSS 解析失败: %s", exc)
            return

        items = root.findall(".//item")
        log.info("cryptocurrencyjobs RSS 共 %d 条", len(items))

        for item in items:
            link = (item.findtext("link") or item.findtext("guid") or "").strip()
            raw_title = (item.findtext("title") or "").strip()
            if not link or not raw_title:
                continue

            posted = self._date(item.findtext("pubDate"))
            stamp = posted.isoformat() if posted else ""
            if stamp > highest:
                highest = stamp
            if watermark and stamp and stamp <= watermark:
                continue

            m = URL_RE.match(link)
            slug = m.group("slug") if m else link.rstrip("/").rsplit("/", 1)[-1]
            category = m.group("category") if m else None

            title, company = raw_title, None
            tm = TITLE_RE.match(raw_title)
            if tm:
                title = tm.group("title").strip()
                company = tm.group("company").strip()

            description = (item.findtext("description") or "").strip()
            # 描述里常有 "can be done remotely" 之类的措辞，是唯一的远程信号
            remote = "remote" if re.search(r"remotely|remote", description, re.I) else None

            yield Job(
                source=self.name,
                source_id=slug,
                url=link,
                title=title,
                description=description or None,
                company_name=company,
                remote_type=remote,
                tags=[category] if category else [],
                posted_at=posted,
                raw={
                    "title": raw_title,
                    "link": link,
                    "description": description,
                    "pubDate": item.findtext("pubDate"),
                    "category": category,
                },
            )

        self.next_cursor = {"max_pub_date": highest}

    @staticmethod
    def _date(value) -> datetime | None:
        """RSS 的 pubDate 是 RFC 2822 格式（Fri, 24 Jul 2026 18:48:17 +0200）。"""
        if not value:
            return None
        try:
            return parsedate_to_datetime(str(value).strip())
        except (TypeError, ValueError):
            return None
