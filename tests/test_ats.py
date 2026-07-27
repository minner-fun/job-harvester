"""ATS：三家看板 API 的解析，以及「一家挂掉不能拖垮整轮」。

这个源的价值在于拿一手数据——同一家公司在聚合站上可能只有 3 个岗位，
而它自己的 Ashby 看板上有 122 个。代价是要同时伺候三套字段完全不同的 API，
每套都有自己的坑：

- **Ashby**：`isListed=False` 是已下架但接口仍返回的岗位；
  `secondaryLocations` 的元素是 `{location, address}` **对象**而非字符串，
  直接 `str()` 会把整个字典塞进地点字段；薪资藏在
  `compensationTiers[].components[]` 里，且同一 tier 下混着股权等非薪资项。
- **Lever**：地点/职能都在 `categories` 里，`salaryRange` 经常整块为 null。
- **Greenhouse**：`content` 是**被 HTML 实体转义过的 HTML**，要先 unescape
  再去标签；而且它不给结构化的远程标记，只能从地点文本里认。
"""

from __future__ import annotations

import pytest
from conftest import FIXTURES, FakeFetcher, collect, load

from job_harvester.sources.ats import ASHBY, GREENHOUSE, LEVER, AtsSource

ASHBY_URL = ASHBY.format("acme-labs")
LEVER_URL = LEVER.format("borealis")
GREENHOUSE_URL = GREENHOUSE.format("cinder")

ROUTES = {
    ASHBY_URL: load("ats", "ashby.json"),
    LEVER_URL: load("ats", "lever.json"),
    GREENHOUSE_URL: load("ats", "greenhouse.json"),
}


@pytest.fixture(autouse=True)
def 看板配置(monkeypatch):
    monkeypatch.setenv("ATS_BOARDS_CONFIG", str(FIXTURES / "ats" / "boards.toml"))


async def test_逐家看板各发一次请求():
    fetcher = FakeFetcher(ROUTES)
    source = AtsSource(fetcher)

    jobs = await collect(source)

    # 5 条配置里 2 条无效（未知 ats / 缺 board），只应请求 3 家
    assert fetcher.requests == 3
    assert {url for url, _ in fetcher.calls} == set(ROUTES)
    # cursor 记的是配置里的看板总数，无效的也算
    assert source.next_cursor == {"boards": 5}
    assert len(jobs) == 6


async def test_单家看板失败不中断整轮():
    routes = dict(ROUTES)
    routes[LEVER_URL] = RuntimeError("模拟 500")
    fetcher = FakeFetcher(routes)

    jobs = await collect(AtsSource(fetcher))

    # Lever 那家没了，另外两家照常
    assert {j.raw["_ats"] for j in jobs} == {"ashby", "greenhouse"}
    assert len(jobs) == 4


async def test_源_id_带看板前缀避免跨看板撞号():
    fetcher = FakeFetcher(ROUTES)
    jobs = await collect(AtsSource(fetcher))

    ids = [j.source_id for j in jobs]
    assert len(ids) == len(set(ids))
    assert "ashby:acme-labs:25b05625-bcd6-4ac0-b73c-f226b380a0dd" in ids
    assert "lever:borealis:1f0a0000-0000-0000-0000-000000000001" in ids
    assert "greenhouse:cinder:4567890" in ids


# ---------------------------------------------------------------------- Ashby
async def test_ashby_过滤已下架岗位():
    fetcher = FakeFetcher(ROUTES)
    jobs = [j for j in await collect(AtsSource(fetcher)) if j.raw["_ats"] == "ashby"]

    assert [j.title for j in jobs] == ["Account Executive (TradFi Segment)", "Support Engineer"]
    assert "Retired Role" not in [j.title for j in jobs]


async def test_ashby_次要地点是对象要取其中的_location():
    fetcher = FakeFetcher(ROUTES)
    job = next(j for j in await collect(AtsSource(fetcher)) if j.title.startswith("Account"))

    assert job.locations == ["New York", "London", "Remote - EMEA"]
    # 回归护栏：字典被整个 str() 进去时一定会带上花括号
    assert not any("{" in loc for loc in job.locations)


async def test_ashby_薪资只认_Salary_组件():
    """同一 tier 下混着股权项，取错会把 0.1% 当成薪资写进库。"""
    fetcher = FakeFetcher(ROUTES)
    job = next(j for j in await collect(AtsSource(fetcher)) if j.title.startswith("Account"))

    assert (job.salary_min, job.salary_max) == (120000, 160000)
    assert job.salary_currency == "USD"
    assert job.salary_period == "annual"


async def test_ashby_薪资整块为空时不报错():
    fetcher = FakeFetcher(ROUTES)
    job = next(j for j in await collect(AtsSource(fetcher)) if j.title == "Support Engineer")

    assert job.salary_min is None
    assert job.salary_currency is None


async def test_ashby_远程标记回退到_isRemote():
    fetcher = FakeFetcher(ROUTES)
    jobs = {j.title: j for j in await collect(AtsSource(fetcher))}

    # workplaceType 有值时以它为准
    assert jobs["Account Executive (TradFi Segment)"].remote_type == "remote"
    # workplaceType 为 null 时才看 isRemote
    assert jobs["Support Engineer"].remote_type == "remote"


async def test_ashby_字段映射():
    fetcher = FakeFetcher(ROUTES)
    job = next(j for j in await collect(AtsSource(fetcher)) if j.title.startswith("Account"))

    assert job.company_name == "Acme Labs"          # 用配置里的公司名
    assert job.employment_type == "full-time"       # "FullTime" 归一
    assert job.tags == ["GTM", "Sales"]             # department + team
    assert job.url == "https://jobs.ashbyhq.com/acme-labs/25b05625"
    assert job.posted_at.isoformat() == "2026-07-01T14:58:07.403000+00:00"


# ---------------------------------------------------------------------- Lever
async def test_lever_字段映射():
    fetcher = FakeFetcher(ROUTES)
    job = next(j for j in await collect(AtsSource(fetcher)) if j.title == "Senior Backend Engineer")

    assert job.company_name == "Borealis"
    assert job.employment_type == "full-time"       # categories.commitment
    assert job.remote_type == "remote"
    assert job.locations == ["United Kingdom - Remote", "GB"]
    assert job.tags == ["Engineering", "Software Engineering"]
    assert (job.salary_min, job.salary_max) == (90000, 130000)
    assert job.salary_currency == "GBP"
    assert job.salary_period == "annual"
    # descriptionPlain + additionalPlain 拼接
    assert job.description == "Build the API.\n\nWe offer equity."
    assert job.posted_at.isoformat() == "2026-07-25T17:20:00+00:00"


async def test_lever_带空格的雇佣类型也要归一():
    """回归护栏：Lever 的 commitment 实测是 "Full Time"（带空格）。

    ats 曾自己维护一张映射表，只有 "fulltime"/"full-time" 两种写法，
    带空格的落不进去、直接变成 NULL，实测丢了 8 条。
    现在交给 models._norm_employment，它会先把空格归一成连字符。
    """
    fetcher = FakeFetcher(ROUTES)
    jobs = {j.title: j for j in await collect(AtsSource(fetcher))}

    assert jobs["Senior Backend Engineer"].employment_type == "full-time"
    assert jobs["Contract Designer"].employment_type == "contract"
    # Ashby 的驼峰写法同样要认
    assert jobs["Account Executive (TradFi Segment)"].employment_type == "full-time"
    assert jobs["Support Engineer"].employment_type == "internship"


async def test_lever_薪资为_null_时不报错():
    fetcher = FakeFetcher(ROUTES)
    job = next(j for j in await collect(AtsSource(fetcher)) if j.title == "Contract Designer")

    assert job.salary_min is None
    assert job.salary_currency is None
    assert job.employment_type == "contract"
    assert job.remote_type == "hybrid"


# ----------------------------------------------------------------- Greenhouse
async def test_greenhouse_描述要先反转义再去标签():
    """content 是被实体转义过的 HTML，少一步就会把 &lt;p&gt; 原样入库。"""
    fetcher = FakeFetcher(ROUTES)
    job = next(j for j in await collect(AtsSource(fetcher)) if j.title == "Security Engineer")

    assert "&lt;" not in job.description
    assert "<p>" not in job.description
    # selectolax 用 separator 连接**所有**文本节点，所以行内的 <strong>
    # 也会被切成独立一行。当前实现如此，这里如实钉住；真要改成
    # 「只在块级元素之间换行」是另一件事，会影响所有源的描述字段。
    assert job.description == "Keep the\nattackers\nout.\nThreat modeling"


async def test_greenhouse_远程只能从地点文本推断():
    fetcher = FakeFetcher(ROUTES)
    jobs = {j.title: j for j in await collect(AtsSource(fetcher))}

    assert jobs["Security Engineer"].remote_type == "remote"   # "Remote - United States"
    assert jobs["Office Manager"].remote_type is None          # "Berlin, Germany"


async def test_greenhouse_字段映射():
    fetcher = FakeFetcher(ROUTES)
    job = next(j for j in await collect(AtsSource(fetcher)) if j.title == "Security Engineer")

    # 接口自己给了 company_name 就用它，不用配置里的
    assert job.company_name == "Cinder Inc."
    assert job.locations == ["Remote - United States", "Remote"]
    assert job.tags == ["Security", "Engineering"]
    assert job.url == "https://job-boards.greenhouse.io/cinder/jobs/4567890"
    # first_published 优先于 updated_at
    assert job.posted_at.isoformat() == "2026-07-10T12:00:00-04:00"


async def test_greenhouse_无_first_published_时回退到_updated_at():
    fetcher = FakeFetcher(ROUTES)
    job = next(j for j in await collect(AtsSource(fetcher)) if j.title == "Office Manager")

    assert job.posted_at.isoformat() == "2026-07-18T08:00:00+00:00"


async def test_每轮全量枚举_可据此判下架():
    """enumerates_all 决定运维层敢不敢用「本轮未见到」判失效。

    这个源每轮把每个看板的全部岗位取一遍，满足前提；
    改成 False 会让下架岗位永远留在库里，改错成 True 的源则会被整库误杀。
    """
    assert AtsSource.enumerates_all is True


async def test_缺配置时不报错只是空转(monkeypatch, tmp_path):
    monkeypatch.setenv("ATS_BOARDS_CONFIG", str(tmp_path / "不存在.toml"))
    fetcher = FakeFetcher(ROUTES)

    assert await collect(AtsSource(fetcher)) == []
    assert fetcher.requests == 0
