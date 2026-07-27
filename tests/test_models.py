"""Job 模型：归一化与变更检测。

`content_hash` 是所有源共用的变更信号——运维层靠它区分「这条记录真的变了」
和「只是这一轮又见到它」。两个方向都会出事：

- 把噪声字段（浏览量、申请数）纳进哈希 → 每轮都判定为已更新，
  `updated_at` 天天在变，变更历史彻底失去意义。
- 把真正的业务字段漏出哈希 → 岗位改了薪资也不会被认出来。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from job_harvester.models import Job, clean_money, ts_from_millis, ts_from_seconds


def 造(**kwargs) -> Job:
    base = {"source": "t", "source_id": "1", "title": "Engineer"}
    return Job(**{**base, **kwargs})


@pytest.mark.parametrize(
    ("原值", "期望"),
    [
        ("Full Time", "full-time"),
        ("FullTime", "full-time"),
        ("full_time", "full-time"),
        ("FULL-TIME", "full-time"),
        ("全职", "full-time"),
        ("Part Time", "part-time"),
        ("兼职", "part-time"),
        ("Contractor", "contract"),
        ("Intern", "internship"),
        ("实习", "internship"),
        ("Temporary", "temporary"),
        (None, None),
        # 认不出来的原样保留，别把信息丢掉
        ("Seasonal", "seasonal"),
    ],
)
def test_雇佣类型归一(原值, 期望):
    assert 造(employment_type=原值).employment_type == 期望


@pytest.mark.parametrize(
    ("原值", "期望"),
    [
        ("YEAR", "annual"), ("yearly", "annual"), ("annual", "annual"), ("年薪", "annual"),
        ("MONTH", "monthly"), ("月", "monthly"),
        ("hour", "hourly"), ("时薪", "hourly"),
        ("day", "daily"), ("week", "weekly"),
        (None, None),
    ],
)
def test_薪资周期归一(原值, 期望):
    assert 造(salary_period=原值).salary_period == 期望


@pytest.mark.parametrize(
    ("原值", "期望"),
    [
        (180000, 180000.0),
        ("180000", 180000.0),
        (0, None),        # 数字 0 表示未知
        ("0", None),      # 字符串 "0" 同理
        (-5, None),
        ("面议", None),
        (None, None),
    ],
)
def test_薪资清洗(原值, 期望):
    assert clean_money(原值) == 期望


def test_币种统一大写():
    assert 造(salary_currency=" usd ").salary_currency == "USD"


def test_数组字段去重去空():
    job = 造(
        tags=["web3", "web3", " ", "", "defi", None],
        locations=["Remote", "Remote"],
        seniority=[],
    )
    assert job.tags == ["web3", "defi"]
    assert job.locations == ["Remote"]
    assert job.seniority == []


# ------------------------------------------------------------- content_hash
def test_同样的内容哈希相同():
    assert 造().content_hash() == 造().content_hash()


@pytest.mark.parametrize(
    "字段",
    [
        "title", "description", "company_name", "employment_type", "remote_type",
        "salary_min", "salary_max", "salary_currency", "url",
    ],
)
def test_业务字段变化会改变哈希(字段):
    改后 = {"salary_min": 999, "salary_max": 9999}.get(字段, "改过了")
    assert 造().content_hash() != 造(**{字段: 改后}).content_hash()


@pytest.mark.parametrize("字段", ["tags", "locations", "seniority"])
def test_数组字段变化会改变哈希(字段):
    assert 造().content_hash() != 造(**{字段: ["新值"]}).content_hash()


def test_发布时间变化会改变哈希():
    早 = 造(posted_at=datetime(2026, 7, 1, tzinfo=timezone.utc))
    晚 = 造(posted_at=datetime(2026, 7, 2, tzinfo=timezone.utc))
    assert 早.content_hash() != 晚.content_hash()


def test_raw_里的噪声不进哈希():
    """源站常在 raw 里塞浏览量、申请数，纳进哈希会导致每轮都判定为已更新。"""
    冷 = 造(raw={"viewCount": 1, "applyCount": 0})
    热 = 造(raw={"viewCount": 99999, "applyCount": 42})

    assert 冷.content_hash() == 热.content_hash()


def test_联系方式不进哈希():
    """联系方式是隔离存储的，变动不该把岗位记录整条标成已更新。"""
    无 = 造()
    有 = 造(contact={"email": "a@example.invalid"})

    assert 无.content_hash() == 有.content_hash()


def test_数组顺序不同则哈希不同():
    """去重保序，所以顺序是有意义的信息，不该被抹平。"""
    assert 造(tags=["a", "b"]).content_hash() != 造(tags=["b", "a"]).content_hash()


# ---------------------------------------------------------------- 时间戳
def test_毫秒时间戳():
    assert ts_from_millis(1785000000000).isoformat() == "2026-07-25T17:20:00+00:00"


def test_秒时间戳():
    assert ts_from_seconds(1785000000).isoformat() == "2026-07-25T17:20:00+00:00"


@pytest.mark.parametrize("坏值", [None, 0, "", "不是数字", float("inf")])
def test_坏时间戳返回_None(坏值):
    assert ts_from_millis(坏值) is None
    assert ts_from_seconds(坏值) is None


def test_时间戳带时区():
    """入库列是 timestamptz，naive datetime 会被当成服务器本地时间。"""
    assert ts_from_seconds(1785000000).tzinfo is not None
    assert ts_from_millis(1785000000000).tzinfo is not None
