"""统一岗位模型：各源 adapter 的输出契约。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone


def _norm_period(value: str | None) -> str | None:
    """薪资周期归一：各源写法不一（YEAR / annual / yearly / 年）。"""
    if not value:
        return None
    v = value.strip().lower()
    if v in {"year", "yearly", "annual", "annually", "年", "年薪"}:
        return "annual"
    if v in {"month", "monthly", "月", "月薪"}:
        return "monthly"
    if v in {"hour", "hourly", "时薪"}:
        return "hourly"
    if v in {"day", "daily"}:
        return "daily"
    if v in {"week", "weekly"}:
        return "weekly"
    return v


def _norm_employment(value: str | None) -> str | None:
    if not value:
        return None
    v = value.strip().lower().replace("_", "-").replace(" ", "-")
    mapping = {
        "full-time": "full-time",
        "fulltime": "full-time",
        "全职": "full-time",
        "part-time": "part-time",
        "parttime": "part-time",
        "兼职": "part-time",
        "contract": "contract",
        "contractor": "contract",
        "合同": "contract",
        "internship": "internship",
        "intern": "internship",
        "实习": "internship",
        "freelance": "freelance",
        "temporary": "temporary",
    }
    return mapping.get(v, v)


def _clean_money(value) -> float | None:
    """薪资清洗。

    RemoteOK 大量返回字符串 "0" 表示「未知」而非零薪，必须转 NULL，
    否则薪资统计会被大量 0 值污染。
    """
    if value is None:
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if num <= 0:
        return None
    return num


@dataclass
class Job:
    source: str
    source_id: str
    title: str
    url: str | None = None
    description: str | None = None

    company_name: str | None = None
    company_website: str | None = None
    company_logo: str | None = None

    employment_type: str | None = None
    remote_type: str | None = None
    seniority: list[str] = field(default_factory=list)
    locations: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)

    salary_min: float | None = None
    salary_max: float | None = None
    salary_currency: str | None = None
    salary_period: str | None = None

    posted_at: datetime | None = None
    expires_at: datetime | None = None

    raw: dict = field(default_factory=dict)

    # 招聘方的联系方式。单独放一个字段，是为了让下游能方便地隔离存储
    # 或直接丢弃 —— 这类数据的处理方式因使用场景而异。
    contact: dict | None = None

    def __post_init__(self) -> None:
        self.employment_type = _norm_employment(self.employment_type)
        self.salary_period = _norm_period(self.salary_period)
        self.salary_min = _clean_money(self.salary_min)
        self.salary_max = _clean_money(self.salary_max)
        if self.salary_currency:
            self.salary_currency = self.salary_currency.strip().upper() or None
        # 去重去空
        self.seniority = _dedupe(self.seniority)
        self.locations = _dedupe(self.locations)
        self.tags = _dedupe(self.tags)

    def content_hash(self) -> str:
        """归一后字段的哈希，用于判断记录是否真的变了。

        不含 raw：源站常在 raw 里塞浏览量、申请数等噪声字段，
        纳入哈希会导致每轮都判定为「已更新」。
        """
        payload = {
            "title": self.title,
            "description": self.description,
            "company_name": self.company_name,
            "employment_type": self.employment_type,
            "remote_type": self.remote_type,
            "seniority": self.seniority,
            "locations": self.locations,
            "tags": self.tags,
            "salary_min": self.salary_min,
            "salary_max": self.salary_max,
            "salary_currency": self.salary_currency,
            "salary_period": self.salary_period,
            "posted_at": self.posted_at.isoformat() if self.posted_at else None,
            "url": self.url,
        }
        blob = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _dedupe(items) -> list[str]:
    out, seen = [], set()
    for it in items or []:
        s = str(it).strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def ts_from_millis(value) -> datetime | None:
    """毫秒时间戳 -> aware datetime（dejob.ai 用毫秒）。"""
    if not value:
        return None
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def ts_from_seconds(value) -> datetime | None:
    """秒时间戳 -> aware datetime（Himalayas / RemoteOK 用秒）。"""
    if not value:
        return None
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None
