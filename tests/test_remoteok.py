"""RemoteOK：首元素不是岗位、`"0"` 薪资表示未知、标签切片去重。

三个实测结论：

1. 响应数组**第一个元素是 `{last_updated, legal}` 说明对象**，没有 `id`。
   当成岗位入库会得到一条标题为空的垃圾记录。
2. `salary_min` / `salary_max` 大量是字符串 `"0"`，含义是「未知」而非零薪。
   不转 NULL 的话，薪资统计会被一大批 0 值拉平。
3. 用标签切片扩大窗口，同一岗位会在多个标签下重复出现，必须按 id 去重。
   而且**不能像 Himalayas 那样 break**——每个标签的结果各自独立排序，
   在标签 A 里遇到旧记录，不代表标签 B 后面没有新的。
"""

from __future__ import annotations

from conftest import FakeFetcher, collect, load

from job_harvester.sources.remoteok import API, TAGS, RemoteOkSource

DEFAULT = load("remoteok", "default.json")
TAGGED = load("remoteok", "tagged.json")


def by_tag(params: dict):
    """带 tags 参数时返回另一份切片，用来测跨切片去重。"""
    return TAGGED if params.get("tags") else DEFAULT


async def test_跳过首个说明对象():
    fetcher = FakeFetcher({API: DEFAULT})
    jobs = await collect(RemoteOkSource(fetcher), full=True)

    # 说明对象没有 id，标题为空的那条也应丢弃，只剩 3 条真岗位
    assert [j.title for j in jobs] == [
        "Staff Platform Engineer",
        "Community Manager",
        "Data Engineer",
    ]
    assert all(j.source_id for j in jobs)


async def test_零薪资视为未知而非零():
    """两种 0 都要清成 NULL，且币种/周期必须跟着清洗后的金额走。

    fixture 里刻意放了两种形态：`"0"`（字符串）和 `0`（数字）。
    前者在 Python 里是**真值**，照着原始字段判真假会写出
    「金额 NULL、币种却是 USD」的记录。
    """
    fetcher = FakeFetcher({API: DEFAULT})
    jobs = await collect(RemoteOkSource(fetcher), full=True)
    by_title = {j.title: j for j in jobs}

    paid = by_title["Staff Platform Engineer"]
    assert (paid.salary_min, paid.salary_max) == (180000, 220000)
    assert paid.salary_currency == "USD"
    assert paid.salary_period == "annual"

    for title in ("Community Manager", "Data Engineer"):
        job = by_title[title]
        assert job.salary_min is None, title
        assert job.salary_max is None, title
        # 没有薪资就不该编造币种和周期
        assert job.salary_currency is None, title
        assert job.salary_period is None, title


async def test_跨标签切片按_id_去重():
    fetcher = FakeFetcher({API: by_tag})
    source = RemoteOkSource(fetcher)

    jobs = await collect(source, full=True)

    ids = [j.source_id for j in jobs]
    assert len(ids) == len(set(ids)), "同一岗位在多个标签下重复出现时应去重"
    # 默认窗口 3 条 + 带标签切片里独有的 1 条；1131402 在两边都有，只留一次
    assert sorted(ids) == ["1131402", "1131403", "1131405", "1131500"]
    # 每个标签一次请求
    assert fetcher.requests == len(TAGS)


async def test_水位线跳过旧记录但不中断后续切片():
    """关键回归：遇到旧记录用 continue 而非 break。

    水位线设在默认窗口最新的那条上。若实现改成 break，
    带标签切片里那条更新的 1131500 就再也抓不到了。
    """
    fetcher = FakeFetcher({API: by_tag})
    source = RemoteOkSource(fetcher, {"max_epoch": 1785000000})

    jobs = await collect(source)

    assert [j.source_id for j in jobs] == ["1131500"]
    assert source.next_cursor == {"max_epoch": 1785010000}


async def test_单个标签失败不中断整轮():
    """一个切片挂掉只该少一批数据，不该让整轮采集失败。"""
    calls = {"n": 0}

    def flaky(params: dict):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("模拟 429")
        return DEFAULT

    fetcher = FakeFetcher({API: flaky})
    jobs = await collect(RemoteOkSource(fetcher), full=True)

    # 第 2 个标签抛异常，但整轮照常跑完，第 1 个标签的数据都在
    assert len(jobs) == 3
    assert fetcher.requests == len(TAGS)


async def test_字段映射():
    fetcher = FakeFetcher({API: DEFAULT})
    job = (await collect(RemoteOkSource(fetcher), full=True))[0]

    assert job.source == "remoteok"
    assert job.source_id == "1131402"
    assert job.company_name == "Acme Labs"
    assert job.remote_type == "remote"          # 站点只收远程岗
    # location 按逗号切分并去掉空白
    assert job.locations == ["New York", "Remote"]
    # tags 里的空串应被清掉
    assert job.tags == ["engineer", "backend", "senior", "python"]
    assert job.posted_at.isoformat() == "2026-07-25T17:20:00+00:00"
    # company_logo 为空串时回退到 logo，两者都空则为 None
    assert job.company_logo is None
