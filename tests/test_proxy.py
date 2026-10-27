"""代理 Profile 解析和脱敏行为的离线测试。"""

from app.config import Settings
from app.proxy import ProxyResolver


def test_proxy_profile_is_resolved_without_exposing_url_in_job_model(monkeypatch):
    """验证代理解析保留逻辑 Profile 和进程内部使用的完整地址。"""
    monkeypatch.setenv("PROXY_PROFILE_GOOGLE_HK", "socks5://user:password@proxy.example:1080")
    lease = ProxyResolver(Settings()).resolve("google_hk", required=True)
    assert lease is not None
    assert lease.profile == "google_hk"
    # 完整地址仅供进程内部派发使用，不加入任务回调字段。
    assert lease.server == "socks5://user:password@proxy.example:1080"
