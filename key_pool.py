"""API Key 池：多 key 轮询/随机/故障转移，401/402 自动拉黑并切换。"""
import random
import threading
import time


class KeyPool:
    def __init__(self, keys: list[str], strategy: str = "round_robin"):
        self._lock = threading.Lock()
        self._keys = [k.strip() for k in keys if k and k.strip()]
        self._strategy = strategy if strategy in ("round_robin", "random", "failover") else "round_robin"
        self._index = 0
        # key -> 拉黑截止时间戳（401 永久拉黑本次会话，429/402 拉黑一段时间）
        self._banned_until: dict[str, float] = {}
        self._permanent_banned: set[str] = set()

    def __bool__(self):
        return bool(self._keys)

    def _available(self) -> list[str]:
        now = time.monotonic()
        return [
            k for k in self._keys
            if k not in self._permanent_banned and self._banned_until.get(k, 0) <= now
        ]

    def acquire(self) -> str | None:
        """按策略取一个可用 key；全部被拉黑时返回 None。"""
        with self._lock:
            avail = self._available()
            if not avail:
                return None
            if self._strategy == "random":
                return random.choice(avail)
            if self._strategy == "failover":
                return avail[0]
            # round_robin
            key = avail[self._index % len(avail)]
            self._index += 1
            return key

    def report_error(self, key: str, status_code: int | None):
        """按错误类型拉黑 key。"""
        with self._lock:
            if status_code in (401,):
                self._permanent_banned.add(key)  # 无效 key，本会话不再使用
            elif status_code == 402:
                self._banned_until[key] = time.monotonic() + 3600  # 余额不足，拉黑 1 小时
            elif status_code == 429:
                self._banned_until[key] = time.monotonic() + 10  # 限流，拉黑 10 秒
