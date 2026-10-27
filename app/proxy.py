from dataclasses import dataclass
import os
import re
from urllib.parse import urlsplit

from .config import Settings


class ProxyUnavailable(RuntimeError):
    """任务要求代理但服务器没有可用配置时抛出。"""


@dataclass(frozen=True)
class ProxyLease:
    """一次任务解析出的代理租约，仅在 Worker 进程内存中保存。"""

    # 逻辑 Profile 名称，用于排查，不包含敏感地址。
    profile: str
    # 内部使用的完整代理地址，不能写入任务状态或日志。
    server: str


class ProxyResolver:
    """把逻辑代理名称解析为服务器端代理地址。"""

    def __init__(self, settings: Settings) -> None:
        """使用进程配置初始化代理解析器。"""
        self.settings = settings

    def resolve(self, profile: str | None, required: bool = False) -> ProxyLease | None:
        """按逻辑名称读取环境变量并校验代理协议、主机和端口。"""
        if not profile and not required:
            return None
        normalized = re.sub(r"[^A-Za-z0-9]+", "_", profile or "default").upper()
        server = os.getenv(f"PROXY_PROFILE_{normalized}") or self.settings.proxy_default_url
        if not server:
            if required:
                raise ProxyUnavailable(f"proxy profile is not configured: {profile or 'default'}")
            return None
        self._validate_server(server)
        return ProxyLease(profile or "default", server)

    @staticmethod
    def _validate_server(server: str) -> None:
        """拒绝不支持或明显非法的代理地址，避免浏览器收到错误配置。"""
        parsed = urlsplit(server)
        try:
            port = parsed.port
        except ValueError as exc:
            raise ProxyUnavailable("proxy URL contains an invalid port") from exc
        if parsed.scheme not in {"http", "https", "socks5", "socks5h"} or not parsed.hostname or (port is not None and not 1 <= port <= 65535):
            raise ProxyUnavailable("proxy URL must use http, https, socks5, or socks5h and include a host")
