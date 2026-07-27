"""Himalayas：水位线提前退出，以及 limit>=10 时的静默降级。

这个源最值钱的两个实测结论都在这里钉死：

1. 结果严格按 `pubDate` 倒序，所以遇到不新于水位线的记录就能停，
   不必翻完全部 ~10,800 页。回归成「翻完整站」的话，日常一轮会从
   1 次请求涨到上万次——足以被站点封禁，而且不会报任何错。
2. `limit >= 10` 时接口把 `companyName` 换成字面量 `"name"`、
   `companyLogo` 换成 `"thumbnail_url"`，其余字段仍然真实，
   **不报错、不改状态码**。只能靠比对发现，所以必须有测试盯着。
"""

from __future__ import annotations

import logging

from conftest import FakeFetcher, collect, load

from job_harvester.sources.himalayas import API, PAGE_LIMIT, HimalayasSource

JOBS = load("himalayas", "jobs.json")


def paged(items=None, *, degrade_at: int | None = None):
    """按 offset/limit 切页，模拟真实接口。

    `degrade_at`：limit 达到该值时返回占位公司名，用来复现站点的批量抓取降级。
    """
    data = JOBS if items is None else items

    def handler(params: dict):
        offset = int(params.get("offset", 0))
        limit = int(params.get("limit", PAGE_LIMIT))
        page = [dict(item) for item in data[offset : offset + limit]]
        if degrade_at is not None and limit >= degrade_at:
            for item in page:
                item["companyName"] = "name"
                item["companyLogo"] = "thumbnail_url"
        return {"jobs": page, "totalCount": len(data)}

    return handler


async def test_全量翻完所有页():
    fetcher = FakeFetcher({API: paged()})
    source = HimalayasSource(fetcher)

    jobs = await collect(source, full=True)

    # 11 条 fixture 里有 1 条 title 为空，应被丢弃而不是入库成空标题
    assert len(jobs) == 10
    assert all(job.title for job in jobs)
    # 每页 9 条 → 11 条要 2 页；第 2 页只有 2 条，不足一页即结束
    assert fetcher.requests == 2
    assert source.next_cursor == {"max_pub_date": 1785000000}


async def test_水位线命中后提前结束():
    fetcher = FakeFetcher({API: paged()})
    # 水位线正好落在第 5 条上
    source = HimalayasSource(fetcher, {"max_pub_date": 1784600000})

    jobs = await collect(source)

    # 只有严格新于水位线的 4 条被产出，等于水位线的那条即触发停止
    assert [j.title for j in jobs] == [
        "Staff Backend Engineer",
        "Product Designer",
        "Support Specialist",
        "Data Analyst",
    ]
    # 关键：在第 1 页就停了，没有翻到第 2 页
    assert fetcher.requests == 1
    assert source.next_cursor == {"max_pub_date": 1785000000}


async def test_水位线已是最新时一次请求即停():
    fetcher = FakeFetcher({API: paged()})
    source = HimalayasSource(fetcher, {"max_pub_date": 1785000000})

    assert await collect(source) == []
    assert fetcher.requests == 1


async def test_full_忽略水位线():
    fetcher = FakeFetcher({API: paged()})
    source = HimalayasSource(fetcher, {"max_pub_date": 1785000000})

    assert len(await collect(source, full=True)) == 10


async def test_请求参数用降级阈值以下的_limit():
    """PAGE_LIMIT 必须 < 10，否则公司名会被换成占位符。"""
    assert PAGE_LIMIT < 10

    fetcher = FakeFetcher({API: paged()})
    await collect(HimalayasSource(fetcher), full=True)

    assert all(params["limit"] == PAGE_LIMIT for _, params in fetcher.calls)


async def test_字段映射():
    fetcher = FakeFetcher({API: paged()})
    jobs = await collect(HimalayasSource(fetcher), full=True)
    job = jobs[0]

    assert job.source == "himalayas"
    # guid 是规范 URL，直接当去重键
    assert job.source_id == "https://himalayas.app/companies/acme-labs/jobs/staff-backend-engineer"
    assert job.company_name == "Acme Labs"
    assert job.employment_type == "full-time"      # "Full Time" 归一
    assert job.remote_type == "remote"             # 该站只收全远程岗
    assert job.locations == ["United States", "Canada"]
    assert job.seniority == ["Staff"]
    # categories + parentCategories 合并
    assert job.tags == ["Backend", "Distributed-Systems", "Engineering"]
    assert (job.salary_min, job.salary_max) == (180000, 240000)
    assert job.salary_currency == "USD"
    assert job.salary_period == "annual"
    assert job.posted_at.isoformat() == "2026-07-25T17:20:00+00:00"
    assert job.expires_at is not None
    # 联系方式不该凭空出现——该源不提供
    assert job.contact is None


async def test_雇佣类型归一():
    fetcher = FakeFetcher({API: paged()})
    jobs = await collect(HimalayasSource(fetcher), full=True)
    got = {job.title: job.employment_type for job in jobs}

    assert got["Product Designer"] == "contract"
    assert got["Support Specialist"] == "part-time"
    assert got["Data Analyst"] == "internship"
    assert got["Technical Writer"] == "freelance"
    assert got["Recruiter"] == "temporary"


async def test_接口降级时置空公司名并报错(caplog):
    """站点若调低降级阈值，占位值不能被静默写进库。"""
    fetcher = FakeFetcher({API: paged(degrade_at=PAGE_LIMIT)})
    source = HimalayasSource(fetcher)

    with caplog.at_level(logging.ERROR, logger="job_harvester.sources.himalayas"):
        jobs = await collect(source, full=True)

    assert all(job.company_name is None for job in jobs)
    assert all(job.company_logo is None for job in jobs)
    # 其余字段仍然真实，不该跟着一起丢
    assert jobs[0].title == "Staff Backend Engineer"
    assert jobs[0].salary_min == 180000

    assert "占位公司名" in caplog.text
    # 只警告一次，不是每条都刷屏
    assert caplog.text.count("占位公司名") == 1


async def test_返回结构异常时安全终止():
    fetcher = FakeFetcher({API: ["不是字典"]})
    source = HimalayasSource(fetcher)

    assert await collect(source, full=True) == []


async def test_空结果页终止翻页():
    fetcher = FakeFetcher({API: {"jobs": [], "totalCount": 0}})

    assert await collect(HimalayasSource(fetcher), full=True) == []
