"""源 adapter 基类。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

from ..http import Fetcher
from ..models import Job


class Source(ABC):
    """一个源 adapter。

    约定：
    - `name` 唯一，会写进每条记录的 `source` 字段，也是下游存取水位线的键
    - `fetch()` 产出 Job，边抓边吐，不在内存里堆全量
    - 增量由 `cursor` 驱动；实现方自行决定水位线语义并在 `next_cursor` 里回填
    """

    name: str
    #: 默认请求间隔（秒）。窗口小、需高频轮询的源可调低。
    delay: float = 1.0
    #: 逐页抓取型的源（首次全量动辄数小时）置 True，
    #: 运行器会预先载入已入库记录的标记，供 adapter 跳过未变更的页面。
    needs_known_marks: bool = False
    #: known_marks 的索引字段。id 可从 URL 得到的源用 source_id，否则用 raw 里的 _url。
    known_mark_by: str = "source_id"
    #: 产出的记录类型。多数源是岗位级；个别源（如人工维护的看板）
    #: 一行是一家公司、含多个自由文本岗位，无法可靠拆成岗位级。
    record_kind: str = "job"
    #: 该源每轮是否会把当前全部岗位取一遍。只有 True 才能靠「本轮没见到」
    #: 推断岗位已下架；滚动窗口型与水位线增量型的源必须保持 False。
    enumerates_all: bool = False

    def __init__(
        self,
        fetcher: Fetcher,
        cursor: dict | None = None,
        known_marks: dict[str, str] | None = None,
    ) -> None:
        self.fetcher = fetcher
        self.cursor = cursor or {}
        self.next_cursor: dict = dict(self.cursor)
        self.known_marks = known_marks or {}

    @abstractmethod
    def fetch(self, *, full: bool = False) -> AsyncIterator[Job]:
        """产出岗位。full=True 时忽略水位线做全量。"""
        raise NotImplementedError
