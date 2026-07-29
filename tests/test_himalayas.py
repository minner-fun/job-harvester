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

import pytest
from conftest import FakeFetcher, collect, load

from job_harvester.sources.himalayas import (
    API,
    PAGE_LIMIT,
    RESUME_REWIND,
    HimalayasSource,
)

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


# ------------------------------------------------------------ 全量断点续跑
# 全量要打一万多次请求、十来个小时，中途必然会撞上站点甩负载。
# 实测连挂两次：第一次死在第 198 次请求（Cloudflare 520），
# 第二次死在第 673 次（站点连发 HTTP 500）。没有续跑就等于永远跑不完。


async def test_全量边走边记进度():
    fetcher = FakeFetcher({API: paged()})
    source = HimalayasSource(fetcher)

    await collect(source, full=True)

    # 走完了就该清掉，否则下次 --full 会从末尾接着走、等于什么都不抓
    assert source.resume_state == {}
    assert "full_offset" not in source.next_cursor


async def test_全量中断时进度留在_resume_state():
    """resume_state 是**失败也要持久化**的那份，水位线不能这样对待。"""
    炸 = {"n": 0}

    def 第二页就炸(params: dict):
        炸["n"] += 1
        if 炸["n"] > 1:
            raise RuntimeError("模拟站点 500")
        return paged()(params)

    fetcher = FakeFetcher({API: 第二页就炸})
    source = HimalayasSource(fetcher)

    with pytest.raises(RuntimeError):
        await collect(source, full=True)

    # 第 1 页翻完了，进度停在 9；第 2 页才炸的
    assert source.resume_state == {"full_offset": PAGE_LIMIT}
    # 水位线不能因为「已经看到过更新的记录」就推进 —— 后面还有没处理的
    assert "max_pub_date" not in source.resume_state


async def test_全量从上次的_offset_续跑():
    fetcher = FakeFetcher({API: paged()})
    source = HimalayasSource(fetcher, {"full_offset": 900})

    await collect(source, full=True)

    起始 = fetcher.calls[0][1]["offset"]
    assert 起始 == 900 - RESUME_REWIND


async def test_续跑会往回退一段():
    """往回退是为了盖住「过期岗位被摘掉导致整体前移」造成的遗漏。

    新岗位插在最前面只会让旧记录往后漂，从原位续跑最多重复处理、不会漏；
    真正会漏的是反方向，所以退一段。
    """
    assert RESUME_REWIND > 0

    fetcher = FakeFetcher({API: paged()})
    source = HimalayasSource(fetcher, {"full_offset": 50})
    await collect(source, full=True)

    # 退过头也不能变成负数
    assert fetcher.calls[0][1]["offset"] == 0


async def test_没有进度时全量从头开始():
    fetcher = FakeFetcher({API: paged()})
    source = HimalayasSource(fetcher, {"max_pub_date": 1784600000})

    await collect(source, full=True)

    assert fetcher.calls[0][1]["offset"] == 0


async def test_增量不碰全量的续跑进度():
    """回归护栏：两次全量之间必然夹着若干轮 cron 增量。

    增量若把 full_offset 顺手抹掉，续跑就永远等不到，
    全量会一次次从 offset 0 重来。
    """
    fetcher = FakeFetcher({API: paged()})
    source = HimalayasSource(fetcher, {"max_pub_date": 1784600000, "full_offset": 5985})

    await collect(source)

    assert source.next_cursor["full_offset"] == 5985
    assert source.next_cursor["max_pub_date"] == 1785000000
    # 增量本身不产生续跑进度
    assert source.resume_state == {}


async def test_增量不受续跑进度影响_仍从头看():
    """full_offset 只对全量有意义，增量必须照旧从 offset 0 看最新的几页。"""
    fetcher = FakeFetcher({API: paged()})
    source = HimalayasSource(fetcher, {"max_pub_date": 1784600000, "full_offset": 5985})

    await collect(source)

    assert fetcher.calls[0][1]["offset"] == 0


async def test_续跑不得让水位线倒退():
    """回归护栏：这是真事故。

    续跑从 offset 14,326 开始，它**前面**那一万多条更新的岗位这一轮根本
    没被看到。若 highest 从 0 起算，算出来的最大 pubDate 只是续跑区间里的
    最大值，比原水位线还旧 —— 实测一次就把水位线从 07-28 推回了 07-23。
    不丢数据（下一轮增量会把这五天重抓一遍），但白跑一大段。
    """
    fetcher = FakeFetcher({API: paged()})
    # 水位线比 fixture 里任何一条都新，续跑区间怎么算都超不过它
    未来 = 1785000000 + 999999
    source = HimalayasSource(fetcher, {"max_pub_date": 未来, "full_offset": 900})

    await collect(source, full=True)

    assert source.next_cursor["max_pub_date"] == 未来, "水位线倒退了"


async def test_全量遇到更新的记录仍会推进水位线():
    """反过来也要成立：真有更新的就得推上去，不能被旧值卡死。"""
    fetcher = FakeFetcher({API: paged()})
    source = HimalayasSource(fetcher, {"max_pub_date": 1000})

    await collect(source, full=True)

    assert source.next_cursor["max_pub_date"] == 1785000000


async def test_全量用更保守的速率与重试预算():
    """沿用增量的参数，实测压 45 分钟后站点就开始返回 500。"""
    assert HimalayasSource.full_delay > HimalayasSource.delay
    assert HimalayasSource.full_max_retries > 4


async def test_返回结构异常时安全终止():
    fetcher = FakeFetcher({API: ["不是字典"]})
    source = HimalayasSource(fetcher)

    assert await collect(source, full=True) == []


async def test_空结果页终止翻页():
    fetcher = FakeFetcher({API: {"jobs": [], "totalCount": 0}})

    assert await collect(HimalayasSource(fetcher), full=True) == []
