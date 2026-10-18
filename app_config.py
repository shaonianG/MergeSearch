"""配置管理：加载/保存 config.json（位于 exe 或脚本同目录，方便便携使用）。含旧版配置自动迁移。"""
import copy
import json
import os
import sys
import threading

DEFAULT_CONFIG = {
    # 平台化密钥管理：每个平台可启停、可配多个 key
    "platforms": {
        "deepseek": {
            "enabled": True,
            "keys": [],
            "base_url": "https://api.deepseek.com",
        },
        "tavily": {
            "enabled": False,
            "keys": [],
            "base_url": "https://api.tavily.com",
        },
    },
    "key_strategy": "round_robin",  # 平台内多 key 选择: round_robin | random | failover
    # search 跨平台负载均衡
    "load_balance": {
        "strategy": "failover",  # failover(主备) | round_robin(轮询) | random(随机)
        "order": ["tavily", "deepseek"],  # 平台优先级顺序（默认 Tavily 优先，DeepSeek 兜底）
    },
    "server": {
        "host": "127.0.0.1",
        "port": 8765,
        "auto_start": True,
    },
    "proxy": {
        "mode": "auto",  # direct | auto | global
        "address": "http://127.0.0.1:7890",
        # Clash 风格分流规则（每行一条，按顺序匹配，先命中先生效）:
        #   DOMAIN-SUFFIX,google.com,PROXY   域名后缀匹配
        #   DOMAIN,www.example.com,DIRECT    域名精确匹配
        #   DOMAIN-KEYWORD,google,PROXY      域名包含关键词
        #   REGEX,^https?://.*\.google\.,DIRECT  正则匹配完整 URL
        "rules": [
            "DOMAIN-SUFFIX,google.com,PROXY",
            "DOMAIN-SUFFIX,googleapis.com,PROXY",
            "DOMAIN-SUFFIX,openai.com,PROXY",
            "DOMAIN-SUFFIX,anthropic.com,PROXY",
            "DOMAIN-SUFFIX,githubusercontent.com,PROXY",
            "DOMAIN-SUFFIX,deepseek.com,DIRECT",
            "DOMAIN-SUFFIX,tavily.com,DIRECT",
            "DOMAIN-SUFFIX,cn,DIRECT",
            "DOMAIN-SUFFIX,baidu.com,DIRECT",
            "DOMAIN-SUFFIX,aliyun.com,DIRECT",
            "DOMAIN,localhost,DIRECT",
            "DOMAIN,127.0.0.1,DIRECT",
        ],
        "auto_default": "direct",  # 未命中规则时: direct | proxy
    },
    "request": {
        "timeout": 30.0,
        "max_retries": 2,
    },
    "defaults": {
        "search_count": 10,
        "need_content": False,
    },
    "autostart": False,
    "log_level": "INFO",
    "ui_theme": "bootstrap-dark",
}


def _deep_merge(base: dict, override: dict) -> dict:
    result = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def app_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _legacy_rule_to_clash(rule: str, action: str) -> str:
    """把旧版通配符规则转成 Clash 语法。"""
    if rule.startswith("re:"):
        return f"REGEX,{rule[3:]},{action}"
    if rule.startswith("*."):
        return f"DOMAIN-SUFFIX,{rule[2:]},{action}"
    if "*" in rule:
        pattern = "^" + "\\.*".join(p.replace(".", "\\.") for p in rule.split("*")) + "$"
        return f"REGEX,{pattern},{action}"
    return f"DOMAIN,{rule},{action}"


def migrate_config(data: dict) -> dict:
    """旧版配置 -> 新版。旧版特征: 顶层 api_keys / proxy.proxy_rules。"""
    data = copy.deepcopy(data or {})

    if "api_keys" in data:
        keys = data.pop("api_keys", [])
        base_url = data.pop("base_url", "https://api.deepseek.com")
        platforms = data.setdefault("platforms", {})
        ds = platforms.setdefault("deepseek", {})
        ds.setdefault("enabled", True)
        if keys and not ds.get("keys"):
            ds["keys"] = keys
        ds.setdefault("base_url", base_url)

    proxy = data.get("proxy")
    if isinstance(proxy, dict) and ("proxy_rules" in proxy or "direct_rules" in proxy):
        rules = [_legacy_rule_to_clash(r, "PROXY") for r in proxy.pop("proxy_rules", [])]
        rules += [_legacy_rule_to_clash(r, "DIRECT") for r in proxy.pop("direct_rules", [])]
        proxy.setdefault("rules", rules)

    return data


class ConfigManager:
    def __init__(self):
        self._lock = threading.Lock()
        self.path = os.path.join(app_dir(), "config.json")
        self._config = self.load()

    def load(self) -> dict:
        data = {}
        migrated = False
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                data = migrate_config(raw)
                migrated = data != raw
            except (json.JSONDecodeError, OSError):
                data = {}
        self._config = _deep_merge(DEFAULT_CONFIG, data)
        if migrated:
            self.save()  # 迁移后立即落盘
        return self._config

    def save(self, config: dict | None = None):
        with self._lock:
            if config is not None:
                self._config = config
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self._config, f, ensure_ascii=False, indent=2)

    @property
    def config(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._config)


def set_windows_autostart(enable: bool, app_name: str = "MergeSearch") -> bool:
    """设置/取消 Windows 开机自启，返回是否成功。仅在打包后的 exe 上有意义。"""
    if not getattr(sys, "frozen", False):
        return False
    try:
        import winreg

        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
            if enable:
                winreg.SetValueEx(key, app_name, 0, winreg.REG_SZ, f'"{sys.executable}"')
            else:
                try:
                    winreg.DeleteValue(key, app_name)
                except FileNotFoundError:
                    pass
        return True
    except OSError:
        return False
