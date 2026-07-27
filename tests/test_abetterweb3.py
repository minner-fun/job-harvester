"""abetterweb3：Notion 库，一行 = 一家公司而不是一个岗位。

这是唯一 `record_kind = "company"` 的源。原因是该库一行里挤着多个岗位、
全写在自由文本字段（`岗位需求`）里，没法可靠地拆成岗位级记录——硬拆只会
造出一批边界错乱的假岗位。所以整行原样存成公司级记录，由使用方自己读。

其余几个坑：

- 属性值是 Notion 的嵌套数组格式 `[["文本"]]`，带链接时是
  `[["文本", [["a", "url"]]]]`，取值要走 `_text` 而不是直接 str()。
- block 节点有时多包一层 `value.value`，两种形态都得认。
- schema 的属性键是随机短串（`aB1c`），必须先拿 schema 把键映射成中文名，
  硬编码键名换个视图就全错。
- Notion 的 reducer **没有稳定的 offset 游标**，深翻会重复返回同一批，
  所以一次性把 limit 放大取完，并在 `hasMore` 为真时告警。
"""

from __future__ import annotations

import logging

from conftest import FakeFetcher, collect, load

from job_harvester.sources.abetterweb3 import (
    BATCH,
    LOAD_CHUNK,
    QUERY,
    AbetterWeb3Source,
)

SCHEMA = load("abetterweb3", "loadPageChunk.json")
ROWS = load("abetterweb3", "queryCollection.json")
ROUTES = {LOAD_CHUNK: SCHEMA, QUERY: ROWS}


async def test_产出公司级记录而非岗位():
    """record_kind 决定运维层往哪张表写。改成 job 会把公司塞进 jobs 表。"""
    assert AbetterWeb3Source.record_kind == "company"

    fetcher = FakeFetcher(ROUTES)
    rows = await collect(AbetterWeb3Source(fetcher), full=True)

    assert all(isinstance(r, dict) for r in rows)
    # 4 个 blockId 里：1 个在 recordMap 里不存在，1 个没有公司名，都该丢
    assert [r["company"] for r in rows] == ["Acme Labs", "Borealis"]


async def test_先取_schema_再按中文名映射():
    fetcher = FakeFetcher(ROUTES)

    await collect(AbetterWeb3Source(fetcher), full=True)

    # 第一次请求拿 schema，第二次拿数据
    assert [url for url, _ in fetcher.calls] == [LOAD_CHUNK, QUERY]


async def test_字段映射():
    fetcher = FakeFetcher(ROUTES)
    row = (await collect(AbetterWeb3Source(fetcher), full=True))[0]

    assert row["block_id"] == "blk-0001"
    assert row["company"] == "Acme Labs"
    assert row["ticker"] == "ACME"
    assert row["source_url"] == "https://acme-labs.example.com"
    assert row["link"] == "https://jobs.acme-labs.example.com"
    # 多选字段按逗号切开
    assert row["ecosystems"] == ["Ethereum", "Solana"]
    assert row["categories"] == ["DeFi"]
    assert row["job_tags"] == ["工程", "产品"]
    assert row["office_areas"] == ["远程", "新加坡"]
    assert row["experience"] == ["3-5年"]
    # 勾选框
    assert row["is_fulltime"] is True
    assert row["is_remote"] is True
    assert row["has_token_equity"] is True
    assert row["is_intern"] is False
    assert row["is_parttime"] is False
    assert row["has_headhunter"] is False
    # 自由文本原样保留，不做拆分
    assert row["positions_raw"] == "招 Solidity 工程师若干"
    assert row["compensation"] == "薪资面议，弹性工作"
    assert row["apply_raw"] == "投递邮箱见官网"
    assert row["notion_edited_at"].isoformat() == "2026-07-25T17:20:00+00:00"


async def test_属性值带链接时只取文本():
    fetcher = FakeFetcher(ROUTES)
    row = (await collect(AbetterWeb3Source(fetcher), full=True))[1]

    # [["Borealis", [["a", "https://..."]]]] → "Borealis"
    assert row["company"] == "Borealis"


async def test_block_多包一层_value_也要认():
    fetcher = FakeFetcher(ROUTES)
    rows = await collect(AbetterWeb3Source(fetcher), full=True)

    borealis = rows[1]
    assert borealis["block_id"] == "blk-0002"
    assert borealis["ecosystems"] == ["Polkadot"]
    assert borealis["is_remote"] is False


async def test_缺省字段有稳定的默认值():
    """下游按固定列写库，字段缺失时不能是 KeyError 或 None 数组。"""
    fetcher = FakeFetcher(ROUTES)
    row = (await collect(AbetterWeb3Source(fetcher), full=True))[1]

    assert row["ticker"] is None
    assert row["categories"] == []
    assert row["office_areas"] == []
    assert row["is_intern"] is False


async def test_水位线按最后编辑时间():
    fetcher = FakeFetcher(ROUTES)
    source = AbetterWeb3Source(fetcher, {"max_edited_ms": 1784000000000})

    rows = await collect(source)

    assert [r["company"] for r in rows] == ["Acme Labs"]
    assert source.next_cursor == {"max_edited_ms": 1785000000000}


async def test_full_忽略水位线():
    fetcher = FakeFetcher(ROUTES)
    source = AbetterWeb3Source(fetcher, {"max_edited_ms": 1785000000000})

    assert len(await collect(source, full=True)) == 2


async def test_取不完时告警(caplog):
    """reducer 没有稳定游标，深翻会重复返回同一批，只能靠调大 limit。"""
    rows = {
        **ROWS,
        "result": {
            "reducerResults": {
                "collection_group_results": {
                    "blockIds": ["blk-0001"],
                    "hasMore": True,
                }
            }
        },
    }
    fetcher = FakeFetcher({LOAD_CHUNK: SCHEMA, QUERY: rows})

    with caplog.at_level(logging.WARNING, logger="job_harvester.sources.abetterweb3"):
        await collect(AbetterWeb3Source(fetcher), full=True)

    assert "仍有更多行未取回" in caplog.text


async def test_一次请求就要取完():
    """BATCH 必须明显大于该库的行数（约 230），否则会静默丢数据。"""
    assert BATCH >= 500

    fetcher = FakeFetcher(ROUTES)
    await collect(AbetterWeb3Source(fetcher), full=True)

    _, payload = fetcher.calls[1]
    limit = payload["loader"]["reducers"]["collection_group_results"]["limit"]
    assert limit == BATCH


async def test_拿不到_schema_时终止(caplog):
    fetcher = FakeFetcher({LOAD_CHUNK: {"recordMap": {}}, QUERY: ROWS})

    with caplog.at_level(logging.ERROR, logger="job_harvester.sources.abetterweb3"):
        assert await collect(AbetterWeb3Source(fetcher), full=True) == []

    assert "未取到 collection schema" in caplog.text
    # 拿不到 schema 就不该再去请求数据
    assert fetcher.requests == 1
