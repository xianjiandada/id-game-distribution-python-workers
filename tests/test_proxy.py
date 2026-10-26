"""代理 Profile 解析和脱敏行为的离线测试。"""

from app.config import Settings
from app.proxy import ProxyResolver


def test_proxy_profile_is_resolved_without_exposing_url_in_job_model(monkeypatch):
    """验证代理凭据只进入 Playwright 配置，不改变逻辑 Profile 名称。"""
    monkeypatch.setenv("PROXY_PROFILE_GOOGLE_HK", "socks5://user:password@proxy.example:1080")
    lease = ProxyResolver(Settings()).resolve("google_hk", required=True)
    assert lease is not None
    assert lease.profile == "google_hk"
    assert lease.as_playwright_proxy() == {
        "server": "socks5://proxy.example:1080",
        "username": "user",
        "password": "password",
    }
