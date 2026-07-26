"""配置：全部来自环境变量，可用 .env 提供。

这里刻意不含任何存储相关的配置 —— 本项目的职责到「产出标准化记录」为止。
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    user_agent: str
    #: 未在源上单独指定时的默认请求间隔（秒）
    default_delay: float
    timeout: float
    #: 可选出口代理。默认直连；只在某个源开始按 IP 封禁时才需要。
    proxy: str | None

    @classmethod
    def load(cls) -> "Settings":
        return cls(
            user_agent=os.environ.get(
                "USER_AGENT",
                "job-harvester (+https://github.com/minner-fun/job-harvester)",
            ),
            default_delay=float(os.environ.get("REQUEST_DELAY", "1.0")),
            timeout=float(os.environ.get("HTTP_TIMEOUT", "30")),
            proxy=os.environ.get("PROXY_URL") or None,
        )
