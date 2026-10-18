"""代理分流：按请求 URL 决定直连或走代理，规则采用 Clash 语法（每行 TYPE,VALUE,ACTION）。"""
import fnmatch
import ipaddress
import logging
import re
from urllib.parse import urlparse

logger = logging.getLogger("proxy_router")

ACTIONS = {"PROXY": "proxy", "DIRECT": "direct"}


def _parse_rule(line: str):
    """解析一行 Clash 风格规则，返回 (matcher, action)；无法识别返回 None。

    支持: DOMAIN / DOMAIN-SUFFIX / DOMAIN-KEYWORD / IP-CIDR / REGEX / MATCH / GEOIP,CN（视为直连兜底提示，忽略）
    兼容旧写法: *.example.com（后缀）、纯域名（精确）、re: 前缀（正则）。
    """
    line = line.strip()
    if not line or line.startswith("#") or line.startswith("//"):
        return None

    if "," in line:
        parts = [p.strip() for p in line.split(",")]
        rtype = parts[0].upper()
        if rtype == "MATCH" and len(parts) >= 2:
            action = ACTIONS.get(parts[-1].upper())
            return ((lambda url, host: True), action) if action else None
        if len(parts) < 3:
            return None
        value, action = ",".join(parts[1:-1]), ACTIONS.get(parts[-1].upper())
        if action is None or not value:
            return None
        if rtype == "DOMAIN":
            return (lambda url, host, v=value.lower(): host == v), action
        if rtype == "DOMAIN-SUFFIX":
            v = value.lower()
            return (lambda url, host, v=v: host == v or host.endswith("." + v)), action
        if rtype == "DOMAIN-KEYWORD":
            v = value.lower()
            return (lambda url, host, v=v: v in host), action
        if rtype == "IP-CIDR":
            try:
                net = ipaddress.ip_network(value, strict=False)
            except ValueError:
                return None
            def _match(url, host, net=net):
                try:
                    return ipaddress.ip_address(host) in net
                except ValueError:
                    return False
            return _match, action
        if rtype == "REGEX":
            try:
                pattern = re.compile(value, re.IGNORECASE)
            except re.error:
                return None
            return (lambda url, host, p=pattern: bool(p.search(url))), action
        return None  # GEOIP/GEOSITE 等不支持，忽略

    # 兼容旧写法（无 ACTION 时默认 PROXY，便于从旧配置迁移后直接生效）
    if line.startswith("re:"):
        try:
            pattern = re.compile(line[3:], re.IGNORECASE)
        except re.error:
            return None
        return (lambda url, host, p=pattern: bool(p.search(url))), "proxy"
    if "*" in line:
        return (lambda url, host, p=line.lower(): fnmatch.fnmatch(host, p)), "proxy"
    v = line.lower()
    return (lambda url, host, v=v: host == v or host.endswith("." + v)), "proxy"


class ProxyRouter:
    def __init__(self, proxy_cfg: dict):
        self.mode = proxy_cfg.get("mode", "auto")
        self.proxy_address = proxy_cfg.get("address", "").strip()
        self.auto_default = proxy_cfg.get("auto_default", "direct")
        self.rules = []
        for line in proxy_cfg.get("rules", []) or []:
            parsed = _parse_rule(line)
            if parsed:
                self.rules.append(parsed)
            elif line.strip() and not line.strip().startswith(("#", "//")):
                logger.warning("无法解析的分流规则已忽略: %s", line)

    def decide(self, url: str) -> str:
        """返回 'direct' 或 'proxy'。"""
        if self.mode == "direct":
            return "direct"
        if self.mode == "global":
            return "proxy" if self.proxy_address else "direct"

        host = (urlparse(url).hostname or "").lower()
        for matcher, action in self.rules:
            try:
                if matcher(url, host):
                    return action
            except Exception:
                continue
        return "proxy" if (self.auto_default == "proxy" and self.proxy_address) else "direct"

    def proxy_httpx_kwargs(self, url: str):
        """返回 httpx 客户端的代理参数（按目标分流时为每个目标单独建客户端）。"""
        if self.decide(url) == "proxy" and self.proxy_address:
            return {"proxy": self.proxy_address}
        return {}
