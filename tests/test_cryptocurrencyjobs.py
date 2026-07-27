"""Cryptocurrency Jobs：RSS 标题拆分与 URL 结构解析。

标题格式固定是「岗位名 at 公司名」，但公司名本身可能带 " at " 之外的分隔符
（实测存在 `Software Engineer (Indexer Focus) - Shielded at Input | Output`），
所以必须从**右侧**切最后一个 " at "。从左切会把公司名截成 "Input"。

这个源是滚动窗口，只有最新 75 条、没有历史，漏采补不回来，
所以水位线只用来省去重复产出，不能用来「跳过后面的」——RSS 是倒序的，
遇到旧的就 continue 而不是 break（后面仍可能有更新的项，虽然罕见）。
"""

from __future__ import annotations

from conftest import FakeFetcher, collect, load

from job_harvester.sources.cryptocurrencyjobs import FEED, CryptocurrencyJobsSource

FEED_XML = load("cryptocurrencyjobs", "feed.xml")


async def test_解析全部条目():
    fetcher = FakeFetcher({FEED: FEED_XML})
    source = CryptocurrencyJobsSource(fetcher)

    jobs = await collect(source, full=True)

    # 4 条里 1 条标题为空，应丢弃
    assert len(jobs) == 3
    assert fetcher.requests == 1
    assert source.next_cursor == {"max_pub_date": "2026-07-24T18:48:17+02:00"}


async def test_标题从右侧切最后一个_at():
    """公司名里含 " | " 等分隔符时，从左切会把公司名截断。"""
    fetcher = FakeFetcher({FEED: FEED_XML})
    jobs = await collect(CryptocurrencyJobsSource(fetcher), full=True)
    by_id = {j.source_id: j for j in jobs}

    简单 = by_id["chronicle-head-of-operations"]
    assert 简单.title == "Head of Operations"
    assert 简单.company_name == "Chronicle"

    复杂 = by_id["input-output-software-engineer-indexer"]
    assert 复杂.title == "Software Engineer (Indexer Focus) - Shielded"
    assert 复杂.company_name == "Input | Output"


async def test_标题不含_at_时整条当岗位名():
    fetcher = FakeFetcher({FEED: FEED_XML})
    jobs = {j.source_id: j for j in await collect(CryptocurrencyJobsSource(fetcher), full=True)}
    job = jobs["acme-labs-head-of-design"]

    assert job.title == "Head of Design"
    assert job.company_name is None


async def test_从_url_取分类与_slug():
    fetcher = FakeFetcher({FEED: FEED_XML})
    jobs = {j.source_id: j for j in await collect(CryptocurrencyJobsSource(fetcher), full=True)}

    assert jobs["chronicle-head-of-operations"].tags == ["operations"]
    assert jobs["input-output-software-engineer-indexer"].tags == ["engineering"]


async def test_远程标记来自描述文本():
    """接口不给结构化的远程字段，描述里的措辞是唯一信号。"""
    fetcher = FakeFetcher({FEED: FEED_XML})
    jobs = {j.source_id: j for j in await collect(CryptocurrencyJobsSource(fetcher), full=True)}

    assert jobs["chronicle-head-of-operations"].remote_type == "remote"
    # 描述里写的是 onsite，但正则也会命中 "remote" 之外的词吗——这里不含，应为 None
    assert jobs["acme-labs-head-of-design"].remote_type is None


async def test_水位线跳过已见过的条目():
    fetcher = FakeFetcher({FEED: FEED_XML})
    source = CryptocurrencyJobsSource(fetcher, {"max_pub_date": "2026-07-23T09:00:00+02:00"})

    jobs = await collect(source)

    assert [j.source_id for j in jobs] == ["chronicle-head-of-operations"]
    assert source.next_cursor == {"max_pub_date": "2026-07-24T18:48:17+02:00"}


async def test_full_忽略水位线():
    fetcher = FakeFetcher({FEED: FEED_XML})
    source = CryptocurrencyJobsSource(fetcher, {"max_pub_date": "2026-07-24T18:48:17+02:00"})

    assert len(await collect(source, full=True)) == 3


async def test_pubDate_是_RFC2822():
    fetcher = FakeFetcher({FEED: FEED_XML})
    job = (await collect(CryptocurrencyJobsSource(fetcher), full=True))[0]

    assert job.posted_at.isoformat() == "2026-07-24T18:48:17+02:00"


async def test_xml_畸形时不抛异常():
    fetcher = FakeFetcher({FEED: "<rss><channel><item>没闭合"})

    assert await collect(CryptocurrencyJobsSource(fetcher), full=True) == []


async def test_滚动窗口源不得声明全量枚举():
    """声明成 True 会让运维层按「本轮未见到」把整库判下架。

    这个源每轮只能看到最新 75 条，历史岗位本来就不会再出现。
    """
    assert CryptocurrencyJobsSource.enumerates_all is False
