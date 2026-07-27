"""web3.career：一页多个 JobPosting，必须按 URL slug 挑对的那个。

这个源最贵的教训：**详情页含 2 个 JobPosting 块**，第二个是页面下方的
推荐岗位（实测抓 Product Owner 页时，第二块是完全无关的 moomoo 岗位）。
盲取第一个就会把推荐位的内容写进本岗位的记录里，而且不会报任何错。
fixture 里刻意把推荐位排在**前面**，正是为了让「取第一个」的写法必然失败。

另外页面上存在会让 `json.loads` 抛 `Extra data` 的畸形 JSON-LD 块，
必须逐块容错，不能因为一块坏了就丢掉整页。

全量要抓 2.7 万个详情页、耗时数小时，所以「已入库且 lastmod 未变就跳过」
是能否中断续跑的关键。
"""

from __future__ import annotations

from conftest import FakeFetcher, collect, load

from job_harvester.sources.web3career import SITEMAP_INDEX, Web3CareerSource

SM1 = "https://web3.career/sitemap1.xml"
SM2 = "https://web3.career/sitemap2.xml"
JOB1 = "https://web3.career/software-engineer-architect-acme-labs/150923"
JOB2 = "https://web3.career/solidity-engineer-borealis/150900"
JOB3 = "https://web3.career/community-lead-cinder/150800"

ROUTES = {
    SITEMAP_INDEX: load("web3career", "sitemap-index.xml"),
    SM1: load("web3career", "sitemap1.xml"),
    SM2: load("web3career", "sitemap2.xml"),
    JOB1: load("web3career", "job.html"),
    JOB2: load("web3career", "job.html"),
    JOB3: load("web3career", "job.html"),
}


def 抓过的详情页(fetcher: FakeFetcher) -> list[str]:
    return [u for u, _ in fetcher.calls if u not in (SITEMAP_INDEX, SM1, SM2)]


async def test_sitemap_只收岗位页且跨_sitemap_去重():
    fetcher = FakeFetcher(ROUTES)

    await collect(Web3CareerSource(fetcher), full=True)

    详情 = 抓过的详情页(fetcher)
    # /solidity-jobs 是标签聚合页、首页也不是岗位，都要排除；
    # 150923 同时出现在两个 sitemap 里，只应抓一次
    assert sorted(详情) == sorted([JOB1, JOB2, JOB3])


async def test_按_lastmod_倒序优先抓最新():
    """中断续跑时，先补齐最新数据比按 sitemap 原序更有用。"""
    fetcher = FakeFetcher(ROUTES)

    await collect(Web3CareerSource(fetcher), full=True)

    assert 抓过的详情页(fetcher) == [JOB1, JOB2, JOB3]


async def test_按_url_slug_挑出本页岗位():
    """回归护栏：推荐位排在第一个，盲取 postings[0] 必然拿到它。

    fixture 的标题里带 `&amp;`——这不是凑数：JSON-LD 里的 & 一律是实体，
    而 URL slug 是按**反转义后**的标题生成的。少一步 unescape，slug 就会
    多出一个 "amp" 段、永远匹配不上，于是静默退回 postings[0]。
    实测线上 272 条标题含 & 的记录，匹配成功数是 0——保护网整个是摆设。
    """
    fetcher = FakeFetcher(ROUTES)
    jobs = await collect(Web3CareerSource(fetcher), full=True)
    job = {j.source_id: j for j in jobs}["150923"]

    assert job.company_name == "Acme Labs"
    assert job.company_name != "Moomoo"
    # 推荐位的薪资是 1-2，取错会写出这种荒唐数字
    assert (job.salary_min, job.salary_max) == (135000, 276000)


async def test_slug_匹配不依赖块的先后顺序():
    """把两个 JobPosting 的顺序调过来，结果必须一样。"""
    html = ROUTES[JOB1]
    第一块 = html.index('"title": "Growth Marketer"')
    第二块 = html.index('"title": "Software Engineer &amp; Architect"')
    assert 第一块 < 第二块, "fixture 必须把推荐位放在前面，否则这个用例证明不了什么"

    for 顺序 in (html, _交换两块(html)):
        fetcher = FakeFetcher({**ROUTES, JOB1: 顺序})
        jobs = await collect(Web3CareerSource(fetcher), full=True)
        job = {j.source_id: j for j in jobs}["150923"]
        assert job.company_name == "Acme Labs"


def _交换两块(html: str) -> str:
    """把两个 <script type="application/ld+json"> JobPosting 块前后对调。"""
    import re

    块 = re.findall(
        r'<script type="application/ld\+json">\s*\{\s*\n\s*"@context.*?</script>',
        html,
        re.S,
    )
    assert len(块) == 2, f"预期 2 个 JobPosting 块，实际 {len(块)}"
    return html.replace(块[0], "@@占位@@").replace(块[1], 块[0]).replace("@@占位@@", 块[1])


async def test_畸形_JSON_LD_块不影响整页():
    fetcher = FakeFetcher(ROUTES)

    jobs = await collect(Web3CareerSource(fetcher), full=True)

    assert len(jobs) == 3


async def test_水位线跳过更早的_lastmod():
    fetcher = FakeFetcher(ROUTES)
    source = Web3CareerSource(fetcher, {"max_lastmod": "2026-07-24T10:00:00Z"})

    await collect(source)

    assert 抓过的详情页(fetcher) == [JOB1]
    assert source.next_cursor == {"max_lastmod": "2026-07-25T13:07:33Z"}


async def test_已入库且未变更的详情页跳过():
    """全量 2.7 万页耗时数小时，中断后不能从头再来。"""
    known = {"150900": "2026-07-24T10:00:00Z", "150800": "2026-07-23T10:00:00Z"}
    fetcher = FakeFetcher(ROUTES)

    await collect(Web3CareerSource(fetcher, None, known), full=True)

    assert 抓过的详情页(fetcher) == [JOB1]


async def test_已知标记的索引字段声明正确():
    """这个源的 id 能从 URL 直接得到，所以用 source_id 索引。"""
    assert Web3CareerSource.needs_known_marks is True
    assert Web3CareerSource.known_mark_by == "source_id"


async def test_单页失败不中断整轮():
    routes = dict(ROUTES)
    routes[JOB2] = RuntimeError("模拟 403")
    fetcher = FakeFetcher(routes)

    jobs = await collect(Web3CareerSource(fetcher), full=True)

    assert sorted(j.source_id for j in jobs) == ["150800", "150923"]


async def test_某个_sitemap_读失败仍用其余的():
    routes = dict(ROUTES)
    routes[SM1] = RuntimeError("模拟超时")
    fetcher = FakeFetcher(routes)

    jobs = await collect(Web3CareerSource(fetcher), full=True)

    # sitemap2 里的两条还在
    assert sorted(j.source_id for j in jobs) == ["150800", "150923"]


async def test_字段映射():
    fetcher = FakeFetcher(ROUTES)
    job = {j.source_id: j for j in await collect(Web3CareerSource(fetcher), full=True)}["150923"]

    assert job.source == "web3career"
    assert job.url == JOB1
    # &amp; 要还原成 &
    assert job.title == "Software Engineer & Architect"
    assert job.employment_type == "full-time"
    assert job.remote_type == "remote"          # jobLocationType = TELECOMMUTE
    # jobLocation 的地址 + applicantLocationRequirements
    assert job.locations == ["New York, NY, US", "Anywhere"]
    assert job.tags == ["Startups"]             # occupationalCategory 为空只留 industry
    assert job.salary_currency == "USD"
    assert job.salary_period == "annual"
    assert job.posted_at.isoformat() == "2026-07-24T01:34:40+01:00"
    assert job.expires_at.isoformat() == "2026-09-28T01:34:40+01:00"
    # 描述里的 HTML 要去掉
    assert "<p>" not in job.description
    assert "Acme Labs" in job.description
    # raw 带上增量所需的两个内部标记
    assert job.raw["_lastmod"] == "2026-07-25T13:07:33Z"
    assert job.raw["_url"] == JOB1


async def test_无_JSON_LD_时跳过该页():
    routes = dict(ROUTES)
    routes[JOB1] = "<html><body>空页</body></html>"
    fetcher = FakeFetcher(routes)

    assert len(await collect(Web3CareerSource(fetcher), full=True)) == 2
