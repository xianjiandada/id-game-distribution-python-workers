from dataclasses import dataclass
import os
import re
from urllib.parse import urlsplit, unquote

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

    def as_playwright_proxy(self) -> dict[str, str]:
        """拆分代理主机、用户名和密码，生成 Playwright 所需结构。"""
        # 代理地址只在内存中使用，不写入任务状态、回调或日志，避免泄露认证信息。
        parsed = urlsplit(self.server)
        host = parsed.hostname or ""
        server = f"{parsed.scheme}://{host}"
        if parsed.port:
            server += f":{parsed.port}"
        result = {"server": server}
        if parsed.username:
            result["username"] = unquote(parsed.username)
        if parsed.password:
            result["password"] = unquote(parsed.password)
        return result


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
