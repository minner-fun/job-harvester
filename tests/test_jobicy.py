"""Jobicy：无 offset 的滚动窗口，只能靠 geo / industry 切片扩大覆盖面。

接口没有 offset/page，`count` 上限 100。所以覆盖面完全依赖切片数量，
而切片之间会大量重叠——去重逻辑一旦退化，同一岗位会被反复产出。

水位线是 ISO 字符串（`pubDate`），做的是**字符串比较**。这能成立的前提是
所有 pubDate 都带同样的格式与时区偏移；一旦源站改格式，比较会静默失真，
所以这里把格式也一起钉住。
"""

from __future__ import annotations

from conftest import FakeFetcher, collect, load

from job_harvester.sources.jobicy import API, COUNT, GEOS, INDUSTRIES, JobicySource

DEFAULT = load("jobicy", "default.json")
SLICE = load("jobicy", "slice.json")

#: 1 个默认查询 + 各 geo（去掉 None）+ 各 industry
QUERY_COUNT = 1 + len([g for g in GEOS if g]) + len(INDUSTRIES)


def by_slice(params: dict):
    return SLICE if (params.get("geo") or params.get("industry")) else DEFAULT


async def test_按切片枚举且跨切片去重():
    fetcher = FakeFetcher({API: by_slice})
    source = JobicySource(fetcher)

    jobs = await collect(source, full=True)

    assert fetcher.requests == QUERY_COUNT
    ids = [j.source_id for j in jobs]
    assert len(ids) == len(set(ids)), "同一岗位出现在多个切片时应去重"
    # 默认切片 2 条（第 3 条无标题被丢）+ 其他切片独有的 1 条
    assert sorted(ids) == ["144343", "144344", "144400"]
    # 每次都请求满额
    assert all(params["count"] == COUNT for _, params in fetcher.calls)


async def test_水位线跳过旧记录但不中断后续切片():
    fetcher = FakeFetcher({API: by_slice})
    source = JobicySource(fetcher, {"max_pub_date": "2026-07-27T13:55:03+00:00"})

    jobs = await collect(source)

    # 只有更新的那条留下；默认切片里的旧记录被跳过，但没有因此停掉后面的切片
    assert [j.source_id for j in jobs] == ["144400"]
    assert source.next_cursor == {"max_pub_date": "2026-07-28T02:00:00+00:00"}


async def test_单个切片失败不中断整轮():
    calls = {"n": 0}

    def flaky(params: dict):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("模拟 503")
        return SLICE

    fetcher = FakeFetcher({API: flaky})
    jobs = await collect(JobicySource(fetcher), full=True)

    assert sorted(j.source_id for j in jobs) == ["144343", "144400"]
    assert fetcher.requests == QUERY_COUNT


async def test_字段映射():
    fetcher = FakeFetcher({API: DEFAULT})
    jobs = await collect(JobicySource(fetcher), full=True)
    job = {j.source_id: j for j in jobs}["144343"]

    assert job.source == "jobicy"
    assert job.company_name == "Acme Labs"
    assert job.remote_type == "remote"       # 站点只收远程岗
    assert job.employment_type == "full-time"
    assert job.seniority == ["Senior"]
    assert job.locations == ["Ireland", "Remote"]
    assert job.tags == ["Content &amp; Editorial"]
    assert (job.salary_min, job.salary_max) == (90000, 120000)
    assert job.salary_currency == "USD"
    # 'yearly' 由 models 归一成 annual
    assert job.salary_period == "annual"
    assert job.posted_at.isoformat() == "2026-07-27T13:55:03+00:00"


async def test_标量字段也接受列表形态():
    """jobIndustry / jobType / jobLevel 有时是列表、有时是标量。"""
    fetcher = FakeFetcher({API: DEFAULT})
    jobs = await collect(JobicySource(fetcher), full=True)
    job = {j.source_id: j for j in jobs}["144344"]

    assert job.tags == ["Customer Support"]      # 标量 → 单元素列表
    assert job.seniority == ["Mid", "Senior"]    # 列表原样保留
    assert job.employment_type == "contract"     # 取列表首元素


async def test_描述为空时回退到摘要():
    fetcher = FakeFetcher({API: DEFAULT})
    jobs = await collect(JobicySource(fetcher), full=True)
    job = {j.source_id: j for j in jobs}["144344"]

    assert job.description == "Answer the tickets."


async def test_零薪资清成_NULL():
    fetcher = FakeFetcher({API: {"jobs": SLICE["jobs"]}})
    jobs = await collect(JobicySource(fetcher), full=True)
    job = {j.source_id: j for j in jobs}["144400"]

    assert job.salary_min is None
    assert job.salary_max is None


async def test_返回结构异常时跳过该切片():
    fetcher = FakeFetcher({API: ["不是字典"]})

    assert await collect(JobicySource(fetcher), full=True) == []
