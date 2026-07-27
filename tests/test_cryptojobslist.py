"""CryptoJobsList：sitemap 增量 + `__NEXT_DATA__` 解析 + 招聘人信息剔除。

要点：

1. sitemap 里混着 blog 等非岗位 URL，必须按 `/jobs/` 过滤，
   否则会去抓一堆解析不出岗位的页面。
2. 增量靠 `lastmod`：水位线之前的整批跳过，已入库且 lastmod 未变的也跳过。
   这个源用 **`_url`** 而不是 `source_id` 做已知标记的索引——它的 id 是
   mongo id、URL 里根本没有，拿 source_id 索引会一个都对不上、每轮全量重抓。
3. `bossFirstName/bossLastName/bossPicture` 是招聘人的姓名与头像，
   不该跟着 raw 一起进库。
"""

from __future__ import annotations

from conftest import FakeFetcher, collect, load

from job_harvester.sources.cryptojobslist import SITEMAP, CryptoJobsListSource

SITEMAP_XML = load("cryptojobslist", "sitemap.xml")
JOB_HTML = load("cryptojobslist", "job.html")

JOB1 = "https://cryptojobslist.com/jobs/institutional-relations-lead-at-acme-labs"
JOB2 = "https://cryptojobslist.com/jobs/solidity-engineer-at-borealis"
JOB3 = "https://cryptojobslist.com/jobs/community-lead-at-cinder"

ROUTES = {SITEMAP: SITEMAP_XML, JOB1: JOB_HTML, JOB2: JOB_HTML, JOB3: JOB_HTML}


async def test_只抓岗位页忽略博客():
    fetcher = FakeFetcher(ROUTES)
    source = CryptoJobsListSource(fetcher)

    await collect(source, full=True)

    抓过的 = [url for url, _ in fetcher.calls]
    assert 抓过的[0] == SITEMAP
    assert set(抓过的[1:]) == {JOB1, JOB2, JOB3}
    assert not any("/blog/" in u for u in 抓过的)


async def test_水位线跳过更早的_lastmod():
    fetcher = FakeFetcher(ROUTES)
    source = CryptoJobsListSource(fetcher, {"max_lastmod": "2026-07-24T10:00:00.000Z"})

    await collect(source)

    抓过的 = [url for url, _ in fetcher.calls if url != SITEMAP]
    # 只有比水位线更新的那一个
    assert 抓过的 == [JOB1]
    assert source.next_cursor == {"max_lastmod": "2026-07-25T01:30:00.922Z"}


async def test_已入库且_lastmod_未变的跳过():
    """已知标记按 _url 索引——这个源的 id 在 URL 里拿不到。"""
    known = {
        JOB2: "2026-07-24T10:00:00.000Z",
        JOB3: "2026-07-23T10:00:00.000Z",
    }
    fetcher = FakeFetcher(ROUTES)
    source = CryptoJobsListSource(fetcher, None, known)

    await collect(source, full=True)

    抓过的 = [url for url, _ in fetcher.calls if url != SITEMAP]
    assert 抓过的 == [JOB1]


async def test_lastmod_变了就要重抓():
    known = {JOB1: "2020-01-01T00:00:00.000Z"}
    fetcher = FakeFetcher(ROUTES)

    await collect(CryptoJobsListSource(fetcher, None, known), full=True)

    assert JOB1 in [url for url, _ in fetcher.calls]


async def test_已知标记的索引字段声明正确():
    """known_mark_by 改回 source_id 会导致每轮全量重抓 440 个页面。"""
    assert CryptoJobsListSource.needs_known_marks is True
    assert CryptoJobsListSource.known_mark_by == "_url"


async def test_招聘人信息不进_raw():
    fetcher = FakeFetcher(ROUTES)
    job = (await collect(CryptoJobsListSource(fetcher), full=True))[0]

    for field in ("bossFirstName", "bossLastName", "bossPicture"):
        assert field not in job.raw, f"{field} 是招聘人个人信息，不该入库"
    # 其余业务字段照常保留
    assert job.raw["companySlug"] == "acme-labs"
    assert job.raw["_url"] == JOB1
    assert job.raw["_lastmod"] == "2026-07-25T01:30:00.922Z"


async def test_字段映射():
    fetcher = FakeFetcher(ROUTES)
    job = (await collect(CryptoJobsListSource(fetcher), full=True))[0]

    assert job.source == "cryptojobslist"
    assert job.source_id == "6a640b3f3d630dcf5377532c"
    assert job.url == JOB1
    assert job.title == "Institutional Relations Lead"
    assert job.company_name == "Acme Labs"
    assert job.company_website == "https://acme-labs.example.com"
    # employmentType 是列表，取首元素再归一
    assert job.employment_type == "full-time"
    assert job.remote_type == "remote"
    assert job.locations == ["New York", "United States"]
    assert job.tags == ["finance", "web3", "remote"]
    assert (job.salary_min, job.salary_max) == (101000, 238000)
    assert job.salary_currency == "USD"
    assert job.salary_period == "annual"      # unitText "YEAR"
    assert job.posted_at.isoformat() == "2026-07-25T01:30:00.922000+00:00"


async def test_无_NEXT_DATA_时跳过该页():
    routes = dict(ROUTES)
    routes[JOB1] = "<html><body>没有内嵌数据</body></html>"
    fetcher = FakeFetcher(routes)

    jobs = await collect(CryptoJobsListSource(fetcher), full=True)

    # 该页产不出岗位，但另外两页照常
    assert len(jobs) == 2


async def test_单页抓取失败不中断整轮():
    routes = dict(ROUTES)
    routes[JOB2] = RuntimeError("模拟 403")
    fetcher = FakeFetcher(routes)

    jobs = await collect(CryptoJobsListSource(fetcher), full=True)

    assert len(jobs) == 2


async def test_NEXT_DATA_畸形时不抛异常():
    routes = dict(ROUTES)
    routes[JOB1] = '<script id="__NEXT_DATA__" type="application/json">{坏的</script>'
    fetcher = FakeFetcher(routes)

    assert len(await collect(CryptoJobsListSource(fetcher), full=True)) == 2
