"""abetterweb3 — Notion 公开页上的人工策展 Web3 招聘库。

    POST https://www.notion.so/api/v3/loadPageChunk     # 取 collection schema
    POST https://www.notion.so/api/v3/queryCollection   # 取数据行

公开页面，无需认证。

与其他源的根本差异：**一行是「公司」而不是「岗位」**。
多个岗位挤在「岗位需求」一个自由文本字段里，且不同岗位常对应不同投递联系人，例如：

    ————以下岗位投递 @Charlia66
    现货产品经理 / 现货产品运营 / 财务运营
    ————以下岗位投递 @HRcoco
    测试 / DBA / CRM后端开发 Java/Golang

规则拆不干净，所以整行原样存进 notion_companies，不强行拆成岗位级、
也不混进 jobs 主表。

字段映射按 schema 里的中文 `name` 在运行时解析，
不硬编码 Notion 那套不透明的属性键（`<p\\m`、`zU=M` 之类），
因为那些键会随数据库结构调整而变。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import datetime, timezone

from .base import Source

log = logging.getLogger(__name__)

LOAD_CHUNK = "https://www.notion.so/api/v3/loadPageChunk"
QUERY = "https://www.notion.so/api/v3/queryCollection?src=initial_load"

PAGE_ID = "daa09583-0b62-4e96-af46-de63fb9771b9"
COLLECTION_ID = "eed2f550-4e6d-4d6d-8ff6-4c04b8f1546d"
COLLECTION_VIEW_ID = "5b432f85-f757-4962-a712-e1f2cb53b6fa"  # 「最近编辑」视图
SPACE_ID = "872059e7-e563-4099-a134-293b02189904"
BATCH = 500  # 该库约 230 行，一次取完；实测 limit 可直接放大，无需深翻

# schema 中文名 -> 我们的字段名
TEXT_FIELDS = {
    "项目/公司": "company",
    "tikcer": "ticker",
    "岗位需求": "positions_raw",
    "待遇/工作环境": "compensation",
    "投递": "apply_raw",
}
URL_FIELDS = {"来源": "source_url", "link": "link"}
MULTI_FIELDS = {
    "生态": "ecosystems",
    "类型": "categories",
    "岗位标签": "job_tags",
    "办公区域": "office_areas",
    "经验": "experience",
}
BOOL_FIELDS = {
    "实习": "is_intern",
    "兼职": "is_parttime",
    "全职": "is_fulltime",
    "远程": "is_remote",
    "猎头对接": "has_headhunter",
    "币权/NFT": "has_token_equity",
}


class AbetterWeb3Source(Source):
    name = "abetterweb3"
    delay = 1.5  # Notion 官方接口，放慢一些
    record_kind = "company"

    def fetch(self, *, full: bool = False) -> AsyncIterator[dict]:
        return self._iter(full=full)

    async def _iter(self, *, full: bool) -> AsyncIterator[dict]:
        schema = await self._schema()
        if not schema:
            log.error("abetterweb3 未取到 collection schema，终止")
            return
        # {属性键: 中文名}
        key_to_name = {k: (v.get("name") or "") for k, v in schema.items()}
        log.info("abetterweb3 schema 共 %d 个字段", len(key_to_name))

        watermark = 0 if full else int(self.cursor.get("max_edited_ms") or 0)
        highest = watermark

        # Notion 的 reducer 没有稳定的 offset 游标，深翻会重复返回同一批。
        # 该库约 230 行，把 limit 直接放大到 BATCH 一次取完即可。
        payload = await self.fetcher.post_json(
            QUERY,
            {
                "source": {"type": "collection", "id": COLLECTION_ID, "spaceId": SPACE_ID},
                "collectionView": {"id": COLLECTION_VIEW_ID, "spaceId": SPACE_ID},
                "loader": {
                    "type": "reducer",
                    "reducers": {
                        "collection_group_results": {
                            "type": "results",
                            "limit": BATCH,
                            "loadContentCover": False,
                        }
                    },
                    "searchQuery": "",
                    "sort": [],
                    "userTimeZone": "Asia/Shanghai",
                },
            },
        )

        results = (
            (payload.get("result") or {})
            .get("reducerResults", {})
            .get("collection_group_results", {})
        )
        block_ids = results.get("blockIds") or []
        blocks = (payload.get("recordMap") or {}).get("block", {})
        if results.get("hasMore"):
            log.warning(
                "abetterweb3 仍有更多行未取回（limit=%d），请调大 BATCH", BATCH
            )

        for bid in block_ids:
            node = blocks.get(bid)
            if not node:
                continue
            value = node.get("value") or {}
            value = value.get("value", value)

            edited = int(value.get("last_edited_time") or 0)
            highest = max(highest, edited)
            if watermark and edited and edited <= watermark:
                continue

            row = self._to_row(bid, value, key_to_name)
            if row:
                yield row

        self.next_cursor = {"max_edited_ms": highest}

    async def _schema(self) -> dict:
        payload = await self.fetcher.post_json(
            LOAD_CHUNK,
            {
                "pageId": PAGE_ID,
                "limit": 50,
                "cursor": {"stack": []},
                "chunkNumber": 0,
                "verticalColumns": False,
            },
        )
        collections = (payload.get("recordMap") or {}).get("collection", {})
        node = collections.get(COLLECTION_ID)
        if not node:
            return {}
        value = node.get("value") or {}
        value = value.get("value", value)
        return value.get("schema") or {}

    # ---------------------------------------------------------------- 映射
    @classmethod
    def _to_row(cls, block_id: str, value: dict, key_to_name: dict) -> dict | None:
        props = value.get("properties") or {}
        row: dict = {
            "block_id": block_id,
            "company": None,
            "ticker": None,
            "source_url": None,
            "link": None,
            "positions_raw": None,
            "compensation": None,
            "apply_raw": None,
        }
        for field in MULTI_FIELDS.values():
            row[field] = []
        for field in BOOL_FIELDS.values():
            row[field] = False

        for key, raw_value in props.items():
            name = key_to_name.get(key) or ("项目/公司" if key == "title" else "")
            text = cls._text(raw_value)
            if not name:
                continue
            if name in TEXT_FIELDS:
                row[TEXT_FIELDS[name]] = text or None
            elif name in URL_FIELDS:
                row[URL_FIELDS[name]] = text or None
            elif name in MULTI_FIELDS:
                row[MULTI_FIELDS[name]] = [p.strip() for p in text.split(",") if p.strip()]
            elif name in BOOL_FIELDS:
                row[BOOL_FIELDS[name]] = text.strip().lower() in {"yes", "true", "✓"}

        if not row["company"]:
            return None

        row["notion_created_at"] = cls._ts(value.get("created_time"))
        row["notion_edited_at"] = cls._ts(value.get("last_edited_time"))
        row["raw"] = {"properties": props, "id": block_id}
        return row

    @staticmethod
    def _text(node) -> str:
        """Notion 属性值形如 [["文本"]] 或 [["文本", [["a", "url"]]]]。"""
        if not isinstance(node, list):
            return str(node or "")
        parts = []
        for seg in node:
            if isinstance(seg, list) and seg:
                parts.append(str(seg[0]))
        return "".join(parts).strip()

    @staticmethod
    def _ts(millis) -> datetime | None:
        if not millis:
            return None
        try:
            return datetime.fromtimestamp(int(millis) / 1000, tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            return None
