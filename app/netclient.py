"""共享网络客户端：所有后台线程/悬浮窗访问 API 都经过这里。

URL 边界校验：本工具是局域网/虚拟组网客户端，目标被显式限定为
「仅限本机、私网与常见虚拟组网网段」，公网地址与无法解析的地址一律拒绝；
重定向目标必须通过同样校验。
"""
import ipaddress
import json
import socket
import urllib.request
from urllib.parse import urlparse

_ALLOWED_V4_NETS = [
    ipaddress.ip_network(n)
    for n in (
        "127.0.0.0/8",        # 环回
        "10.0.0.0/8",         # 私网（ZeroTier 等）
        "172.16.0.0/12",      # 私网（蒲公英等）
        "192.168.0.0/16",     # 私网
        "169.254.0.0/16",     # 链路本地
        "100.64.0.0/10",      # CGNAT（Tailscale 等）
        "25.0.0.0/8",         # Hamachi
        "26.0.0.0/8",         # Radmin VPN
    )
]


def _host_is_lan(host: str) -> bool:
    """主机名（或 IP 字面量）必须解析到允许的组网地址段。"""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        try:
            ip = ipaddress.ip_address(socket.gethostbyname(host))
        except (OSError, ValueError):
            return False
    if ip.version == 6:
        return ip.is_loopback or ip.is_link_local or ip.is_private
    return any(ip in net for net in _ALLOWED_V4_NETS)


def validate_base_url(url: str) -> str | None:
    """校验用户输入/配置里的服务器地址，非法返回 None。"""
    if not isinstance(url, str) or not url or any(ch.isspace() for ch in url):
        return None
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    # 禁止内嵌凭据、查询串等附加成分，避免 URL 结构被滥用
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        return None
    try:
        port = parsed.port  # 超范围端口会在此抛 ValueError
    except ValueError:
        return None
    if port is not None and not (1 <= port <= 65535):
        return None
    if not _host_is_lan(parsed.hostname):
        return None
    return url.rstrip("/")


class _LANRedirectHandler(urllib.request.HTTPRedirectHandler):
    """重定向目标必须同样通过组网边界校验，否则中止。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if validate_base_url(newurl) is None:
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_LANRedirectHandler())


def _guarded(url: str) -> str:
    validated = validate_base_url(url)
    if validated is None:
        raise ValueError(f"地址不在允许的组网范围内: {url!r}")
    return validated


def get_json(url: str, timeout: float = 5):
    """GET 并解析 JSON；URL 必须通过组网边界校验。"""
    with _opener.open(_guarded(url), timeout=timeout) as r:
        return json.loads(r.read())


def post_json(url: str, body: dict | None = None, timeout: float = 5):
    """POST JSON；body 为 None 时发空体。"""
    data = json.dumps(body).encode() if body is not None else b"{}"
    req = urllib.request.Request(
        _guarded(url), data=data, method="POST",
        headers={"Content-Type": "application/json"})
    with _opener.open(req, timeout=timeout) as r:
        return json.loads(r.read())
