"""命令行入口。

本项目的职责到「产出标准化岗位记录」为止，不涉及任何存储。
采集结果以 NDJSON 写到 stdout，增量水位线通过参数进、通过文件出；
下游要入库、发消息队列还是直接看，由调用方决定。

    harvest list                              # 列出可用源
    harvest info <源名>                       # 源的元信息（JSON）
    harvest fetch <源名> [选项]               # 采集，NDJSON 到 stdout

典型用法：

    # 看看长什么样
    harvest fetch ats --limit 5 | jq -r '.company_name + " | " + .title'

    # 增量：把上次的水位线传进去，把新的水位线写出来
    harvest fetch himalayas \\
        --cursor-in  state/himalayas.json \\
        --cursor-out state/himalayas.json > jobs.ndjson
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from dataclasses import asdict
from pathlib import Path

from .config import Settings
from .http import Fetcher
from .models import Job
from .sources import SOURCES

log = logging.getLogger("job_harvester")


def _setup_logging(verbose: bool) -> None:
    # 日志一律走 stderr，保证 stdout 是干净的 NDJSON，可以直接接管道
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _job_to_dict(job: Job) -> dict:
    d = asdict(job)
    d["posted_at"] = job.posted_at.isoformat() if job.posted_at else None
    d["expires_at"] = job.expires_at.isoformat() if job.expires_at else None
    # 归一化字段的哈希，供下游做变更检测。不含 raw —— 源站常在原始响应里塞
    # 浏览量、申请数这类噪声字段，纳入哈希会导致每轮都判定为「已更新」。
    d["content_hash"] = job.content_hash()
    return d


def _load_cursor(args) -> dict:
    if args.full:
        return {}
    if args.cursor:
        return json.loads(args.cursor)
    if args.cursor_in:
        path = Path(args.cursor_in)
        if path.exists():
            text = path.read_text(encoding="utf-8").strip()
            if text:
                return json.loads(text)
    return {}


async def cmd_fetch(cfg: Settings, args) -> int:
    source_cls = SOURCES.get(args.source)
    if not source_cls:
        log.error("未知源 %r，可用: %s", args.source, ", ".join(sorted(SOURCES)))
        return 2

    cursor = _load_cursor(args)
    known: dict[str, str] = {}
    if args.known_marks:
        path = Path(args.known_marks)
        if path.exists():
            known = json.loads(path.read_text(encoding="utf-8"))

    out = sys.stdout if args.out in (None, "-") else open(args.out, "w", encoding="utf-8")
    fetcher = Fetcher(
        user_agent=cfg.user_agent,
        delay=args.delay if args.delay is not None else (source_cls.delay or cfg.default_delay),
        timeout=cfg.timeout,
        proxy=cfg.proxy,
    )
    source = source_cls(fetcher, cursor, known)

    count = 0
    ok = False
    try:
        async for job in source.fetch(full=args.full):
            out.write(json.dumps(_job_to_dict(job), ensure_ascii=False, default=str) + "\n")
            count += 1
            if count % 500 == 0:
                out.flush()
            if args.limit and count >= args.limit:
                log.info("达到 --limit %d，停止抓取", args.limit)
                break
        ok = True
    except Exception:  # noqa: BLE001 — 顶层要把失败转成退出码
        log.exception("采集失败")
    finally:
        out.flush()
        if out is not sys.stdout:
            out.close()
        await fetcher.close()

    log.info("[%s] 请求 %d 次，产出 %d 条记录", args.source, fetcher.requests, count)

    # 只有成功跑完才写回水位线：失败时保留旧值以便下次重抓；
    # 带 --limit 的调试运行同理，否则会把水位线推到一个不完整的位置。
    if ok and not args.limit and args.cursor_out:
        Path(args.cursor_out).write_text(
            json.dumps(source.next_cursor, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
        log.info("[%s] 水位线已写入 %s: %s", args.source, args.cursor_out, source.next_cursor)
    elif ok:
        log.info("[%s] 水位线: %s", args.source, source.next_cursor)

    return 0 if ok else 1


def cmd_list() -> int:
    import importlib

    for name in sorted(SOURCES):
        cls = SOURCES[name]
        # 说明写在各 adapter 的模块 docstring 首行，类本身没有 docstring
        mod = importlib.import_module(cls.__module__)
        summary = (mod.__doc__ or "").strip().splitlines()[0] if mod.__doc__ else ""
        kind = "" if cls.record_kind == "job" else f"  [{cls.record_kind}]"
        print(f"{name:<20} {summary}{kind}")
    return 0


def cmd_info(name: str) -> int:
    cls = SOURCES.get(name)
    if not cls:
        print(f"未知源 {name!r}", file=sys.stderr)
        return 2
    print(json.dumps({
        "name": cls.name,
        "delay": cls.delay,
        # 该源每轮是否会取回当前全部岗位。下游据此决定能否用
        # 「本轮未再出现」判定岗位已下架 —— 对滚动窗口型和水位线增量型的源
        # 这么做会把整库误杀。
        "enumerates_all": cls.enumerates_all,
        # 需要下游回传已入库记录的标记，以便跳过未变更的详情页
        "needs_known_marks": cls.needs_known_marks,
        "known_mark_by": cls.known_mark_by,
        "record_kind": cls.record_kind,
    }, ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="harvest",
        description="多源岗位采集器：产出标准化岗位记录（NDJSON），不涉及存储",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="列出可用源")

    info = sub.add_parser("info", help="查看源的元信息")
    info.add_argument("source")

    fetch = sub.add_parser("fetch", help="采集，NDJSON 输出到 stdout")
    fetch.add_argument("source", help=f"可用: {', '.join(sorted(SOURCES))}")
    fetch.add_argument("--out", help="输出文件，默认 stdout")
    fetch.add_argument("--cursor", help="水位线 JSON 字面量")
    fetch.add_argument("--cursor-in", help="从文件读水位线；文件不存在视为空")
    fetch.add_argument("--cursor-out", help="成功跑完后把新水位线写到该文件")
    fetch.add_argument("--known-marks", help="已入库记录标记的 JSON 文件，用于跳过未变更页面")
    fetch.add_argument("--full", action="store_true", help="忽略水位线做全量")
    fetch.add_argument("--limit", type=int, help="最多产出多少条（调试用，不写回水位线）")
    fetch.add_argument("--delay", type=float, help="覆盖该源的默认请求间隔（秒）")

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)

    if args.cmd == "list":
        return cmd_list()
    if args.cmd == "info":
        return cmd_info(args.source)
    if args.cmd == "fetch":
        return asyncio.run(cmd_fetch(Settings.load(), args))
    return 2


if __name__ == "__main__":
    sys.exit(main())
