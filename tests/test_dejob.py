"""dejob.ai：水位线按页提前退出、薪资占位值过滤、PII 隔离。

三个容易退化的地方：

1. 列表按 `createTime` 倒序，遇到不新于水位线的即可整轮停止。全量是 47 页，
   退化成每轮翻满 47 页虽然还能用，但纯属浪费。
2. 发布者常用 `min=1 / max=99999999` 表示「面议」。不过滤的话，
   薪资统计里会出现 1 美元和 1 亿美元的月薪。
3. 真实 PII **不在** 列表接口的 email/phone 字段里（那几个实测恒为空），
   而在 `user.nickname` / `user.walletAddress`。这些必须进 `contact`
   而不是散落在 raw 之外的普通字段里，下游才有办法隔离存储或整体丢弃。

注意：本文件 fixture 里的邮箱、钱包地址全是编造的（`.invalid` 域名 +
全零地址），本仓库公开，真实招聘方联系方式不进 git。
"""

from __future__ import annotations

import pytest
from conftest import FakeFetcher, collect, load

from job_harvester.sources import dejob as dejob_mod
from job_harvester.sources.dejob import API, DejobSource

PAGE1 = load("dejob", "page1.json")
PAGE2 = load("dejob", "page2.json")
EMPTY = {"errorCode": 0, "data": {"page": {"total": 5}, "results": None}}


@pytest.fixture(autouse=True)
def 小页长(monkeypatch):
    """把每页 100 条调成 3 条，fixture 才不用堆 100 个岗位来触发翻页。"""
    monkeypatch.setattr(dejob_mod, "PAGE_LIMIT", 3)


def paged(params: dict):
    return {1: PAGE1, 2: PAGE2}.get(int(params.get("page", 1)), EMPTY)


async def test_全量翻页直到不满一页():
    fetcher = FakeFetcher({API: paged})
    source = DejobSource(fetcher)

    jobs = await collect(source, full=True)

    # 第 2 页只有 2 条（< PAGE_LIMIT），据此结束，不必再请求第 3 页
    assert fetcher.requests == 2
    # 5 条里 1 条 positionName 为空，应丢弃
    assert [j.source_id for j in jobs] == ["4016", "4017", "4018", "4019"]
    assert source.next_cursor == {"max_created_ms": 1785000000000}


async def test_水位线命中后提前结束():
    fetcher = FakeFetcher({API: paged})
    source = DejobSource(fetcher, {"max_created_ms": 1784900000000})

    jobs = await collect(source)

    assert [j.source_id for j in jobs] == ["4016"]
    # 关键：在第 1 页就停了，没有翻到第 2 页
    assert fetcher.requests == 1


async def test_末页返回_null_时终止():
    """page 超出末页时 results 是 null，是唯一的结束标志。"""
    fetcher = FakeFetcher({API: lambda p: PAGE1 if int(p["page"]) == 1 else EMPTY})
    source = DejobSource(fetcher)

    # 第 1 页满 3 条 → 继续翻；第 2 页 results=null → 停
    assert len(await collect(source, full=True)) == 3
    assert fetcher.requests == 2


async def test_接口报错时安全终止():
    fetcher = FakeFetcher({API: {"errorCode": 500, "message": "boom"}})

    assert await collect(DejobSource(fetcher), full=True) == []


async def test_薪资占位值被过滤():
    fetcher = FakeFetcher({API: paged})
    jobs = {j.source_id: j for j in await collect(DejobSource(fetcher), full=True)}

    正常 = jobs["4016"]
    assert (正常.salary_min, 正常.salary_max) == (1500, 3000)
    assert 正常.salary_currency == "USD"   # 接口不返回币种，由站点渲染代码坐实
    assert 正常.salary_period == "monthly"

    # min=1 / max=99999999 是「面议」占位：下限低于地板、上限高于天花板、
    # 且比值远超 20 倍，三条都该拦下
    面议 = jobs["4017"]
    assert 面议.salary_min is None
    assert 面议.salary_max is None
    assert 面议.salary_currency is None
    assert 面议.salary_period is None

    # 800 低于 SALARY_FLOOR=100？不，800 合法；但下限合法上限也合法时应保留
    实习 = jobs["4018"]
    assert (实习.salary_min, 实习.salary_max) == (800, 1200)


@pytest.mark.parametrize(
    ("smin", "smax", "expected"),
    [
        (1500, 3000, (1500.0, 3000.0)),      # 正常区间
        (1, 99999999, (None, None)),         # 面议占位
        (50, 3000, (None, None)),            # 下限低于地板
        (2000, 500000, (None, None)),        # 上限高于天花板
        (100, 5000, (None, None)),           # 比值 50 倍，超过 SALARY_MAX_RATIO
        (3000, 1500, (None, None)),          # 上下颠倒
        (1500, None, (None, None)),          # 只剩一端，区间无意义
        ("abc", 3000, (None, None)),         # 非数字
    ],
)
def test_薪资清洗规则(smin, smax, expected):
    assert DejobSource._salary({"minSalary": smin, "maxSalary": smax}) == expected


async def test_联系方式进隔离字段():
    fetcher = FakeFetcher({API: paged})
    jobs = {j.source_id: j for j in await collect(DejobSource(fetcher), full=True)}

    # nickname 是邮箱形态 → 认成 email
    assert jobs["4016"].contact == {
        "email": "hiring@example.invalid",
        "phone": None,
        "telegram": None,
        "wechat": None,
        "wallet_address": None,
    }

    # nickname 不是邮箱、walletAddress 是 0x 地址 → 只记钱包
    assert jobs["4017"].contact == {
        "email": None,
        "phone": None,
        "telegram": None,
        "wechat": None,
        "wallet_address": "0x00000000000000000000000000000000deadbeef",
    }

    # 什么都没有时应是 None，而不是一个全 None 的字典
    assert jobs["4018"].contact is None


async def test_地点取_base_而非_location():
    """该站 location 恒为空，实际地点写在 base 里，且用中文顿号等分隔。"""
    fetcher = FakeFetcher({API: paged})
    jobs = {j.source_id: j for j in await collect(DejobSource(fetcher), full=True)}

    assert jobs["4016"].locations == ["Remote", "北京"]
    assert jobs["4017"].locations == ["上海", "深圳"]
    assert jobs["4018"].locations == []


async def test_字段映射():
    fetcher = FakeFetcher({API: paged})
    job = {j.source_id: j for j in await collect(DejobSource(fetcher), full=True)}["4016"]

    assert job.source == "dejob"
    assert job.company_name == "Acme Labs"
    assert job.company_website == "https://acme-labs.example.com/"
    assert job.employment_type == "full-time"   # "Full/Part" → full-time
    assert job.remote_type == "remote"
    assert job.tags == ["人际沟通", "业务开发"]
    # leverName 是紧急度标记，不是职级，不该进 seniority
    assert job.seniority == []
    assert job.posted_at.isoformat() == "2026-07-25T17:20:00+00:00"
    # 描述由 companyIntroduction + content + content2 + content3 拼成
    assert job.description.startswith("Acme Labs 是一家虚构的")
    assert "职位要求" in job.description
    assert "高额的薪酬奖励" in job.description


async def test_url_缺失时回退到详情页():
    fetcher = FakeFetcher({API: paged})
    jobs = {j.source_id: j for j in await collect(DejobSource(fetcher), full=True)}

    assert jobs["4017"].url == "https://dejob.ai/jobDetail?id=4017"


async def test_雇佣类型与办公方式映射():
    fetcher = FakeFetcher({API: paged})
    jobs = {j.source_id: j for j in await collect(DejobSource(fetcher), full=True)}

    assert jobs["4017"].employment_type == "part-time"
    assert jobs["4017"].remote_type == "hybrid"     # "Remote/On-site"
    assert jobs["4018"].employment_type == "internship"
    assert jobs["4018"].remote_type == "onsite"
