"""用量统计：按平台/工具计数，持久化到 stats.json。"""
import json
import os
import threading
import time
from collections import defaultdict

from app_config import app_dir


class StatsManager:
    def __init__(self):
        self._lock = threading.Lock()
        self.path = os.path.join(app_dir(), "stats.json")
        self._data = self._load()

    def _load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                data.setdefault("since", time.strftime("%Y-%m-%d %H:%M:%S"))
                return data
            except (json.JSONDecodeError, OSError):
                pass
        return {"since": time.strftime("%Y-%m-%d %H:%M:%S"), "counters": {}}

    def _save(self):
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
        except OSError:
            pass

    def record(self, provider: str, tool: str, ok: bool):
        """tool: search | fetch"""
        with self._lock:
            counters = self._data.setdefault("counters", {})
            slot = counters.setdefault(provider, defaultdict(int))
            key = f"{tool}_{'ok' if ok else 'fail'}"
            slot[key] = slot.get(key, 0) + 1
            slot["total"] = slot.get("total", 0) + 1
            self._save()

    def snapshot(self):
        with self._lock:
            counters = {p: dict(c) for p, c in self._data.get("counters", {}).items()}
            return {"since": self._data.get("since", ""), "counters": counters}

    def reset(self):
        with self._lock:
            self._data = {"since": time.strftime("%Y-%m-%d %H:%M:%S"), "counters": {}}
            self._save()
