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
    - 全量若可能中途失败，把进度写进 `resume_state`，下游会连失败一起持久化
    """

    name: str
    #: 默认请求间隔（秒）。窗口小、需高频轮询的源可调低。
    delay: float = 1.0
    #: 全量时的请求间隔与重试预算，None 表示沿用增量的。
    #: 全量要连打上万次请求，站点会开始甩负载 —— Himalayas 实测在
    #: 1 次/秒 压了 45 分钟后开始返回 500，且响应从 1 秒退化到 28 秒。
    #: 沿用增量的参数必然中途被拒。
    full_delay: float | None = None
    full_max_retries: int | None = None
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
        #: 断点续跑进度，**失败时也会被持久化**。
        #:
        #: 和 `next_cursor` 的区别是关键：水位线只有整轮成功才能推进，
        #: 中途失败还推进就会跳过没处理的记录；而「全量走到了第几个 offset」
        #: 是已经花掉的请求，丢了就得从头再走一遍。Himalayas 全量要一万多次
        #: 请求、十来个小时，指望一次不中断地打完是不现实的。
        #:
        #: 下游会把它叠加到**旧**水位线上写回，所以这里只放进度，别放水位线。
        self.resume_state: dict = {}

    @abstractmethod
    def fetch(self, *, full: bool = False) -> AsyncIterator[Job]:
        """产出岗位。full=True 时忽略水位线做全量。"""
        raise NotImplementedError
