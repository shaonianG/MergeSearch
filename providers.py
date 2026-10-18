"""搜索提供商抽象：DeepSeek / Tavily 统一封装，多 key 管理 + 跨平台负载均衡。"""
import asyncio
import itertools
import logging
import random
import threading
import time

import httpx

from proxy_router import ProxyRouter

logger = logging.getLogger("providers")


class ApiError(Exception):
    """统一 API 错误。retryable 表示可重试，auth 表示 key 无效/欠费。"""

    def __init__(self, message, status=0, retryable=False, auth=False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable
        self.auth = auth


class KeyPool:
    """平台内多 key 管理：轮询/随机/主备，失败的 key 临时拉黑。"""

    def __init__(self, keys, strategy="round_robin", blacklist_ttl=300.0):
        self.keys = list(keys)
        self.strategy = strategy
        self.blacklist_ttl = blacklist_ttl
        self._lock = threading.Lock()
        self._index = 0
        self._blacklist = {}  # key -> 解禁时间戳

    def _purge(self):
        import time
        now = time.time()
        self._blacklist = {k: t for k, t in self._blacklist.items() if t > now}

    def acquire(self):
        with self._lock:
            self._purge()
            available = [k for k in self.keys if k not in self._blacklist]
            if not available:
                available = list(self.keys)  # 全部拉黑时兜底重试
            if not available:
                raise ApiError("该平台没有配置 API Key")
            if self.strategy == "random":
                return random.choice(available)
            if self.strategy == "failover":
                return available[0]
            key = available[self._index % len(available)]
            self._index += 1
            return key

    def report_failure(self, key, blacklist=False):
        if not blacklist:
            return
        import time
        with self._lock:
            self._blacklist[key] = time.time() + self.blacklist_ttl
            logger.warning("key ...%s 已临时拉黑 %ds", key[-4:], int(self.blacklist_ttl))


class BaseProvider:
    """单个平台的搜索客户端：httpx 连接池（按直连/代理缓存）+ key 池 + 重试。"""

    name = "base"

    def __init__(self, platform_cfg, request_cfg, router: ProxyRouter):
        self.base_url = platform_cfg.get("base_url", "").rstrip("/")
        self.pool = KeyPool(platform_cfg.get("keys", []), request_cfg.get("key_strategy", "round_robin"))
        self.timeout = float(request_cfg.get("timeout", 30.0))
        self.max_retries = int(request_cfg.get("max_retries", 2))
        self._router = router
        self._clients = {}
        self._clients_lock = threading.Lock()

    def _client(self, url: str) -> httpx.AsyncClient:
        target = self._router.decide(url)
        with self._clients_lock:
            client = self._clients.get(target)
            if client is None:
                mounts = None
                if target == "proxy":
                    proxy_url = self._router.proxy_address
                    mounts = {pattern: httpx.AsyncHTTPTransport(proxy=proxy_url)
                              for pattern in ("http://", "https://")}
                client = httpx.AsyncClient(timeout=self.timeout, mounts=mounts)
                self._clients[target] = client
            return client

    async def _request(self, path, body, timeout=None) -> dict:
        """带 key 轮换与重试的 POST 请求，返回解析后的 JSON。"""
        url = self.base_url + path
        attempts = 1 + self.max_retries + len(self.pool.keys)
        last_error = None
        for attempt in range(attempts):
            try:
                key = self.pool.acquire()
            except ApiError:
                raise
            client = self._client(url)
            try:
                resp = await client.post(url, json=body, headers=self._headers(key),
                                         timeout=timeout or self.timeout)
            except httpx.HTTPError as exc:
                last_error = ApiError(f"网络错误: {exc}", retryable=True)
                logger.warning("%s 第 %d 次请求失败: %s", self.name, attempt + 1, exc)
                await asyncio.sleep(min(2 ** attempt, 4) * 0.5)
                continue
            if resp.status_code < 400:
                return resp.json()
            err = self._classify(resp)
            last_error = err
            if err.auth:
                self.pool.report_failure(key, blacklist=True)
                continue
            if err.retryable:
                await asyncio.sleep(min(2 ** attempt, 4) * 0.5)
                continue
            raise err
        raise last_error or ApiError("请求失败")

    def _headers(self, key):
        return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    def _classify(self, resp) -> ApiError:
        status = resp.status_code
        msg = ""
        try:
            data = resp.json()
            err = data.get("error") or {}
            msg = err.get("message") or data.get("message") or ""
        except ValueError:
            msg = resp.text[:200]
        msg = msg or resp.text[:200] or f"HTTP {status}"
        if status in (401, 403):
            return ApiError(f"key 无效或被封禁（{status}: {msg}）", status, auth=True)
        if status == 402:
            return ApiError(f"余额不足（402: {msg}）", status, auth=True)
        if status in (408, 429) or status >= 500:
            return ApiError(f"HTTP {status}: {msg}", status, retryable=True)
        return ApiError(f"HTTP {status}: {msg}", status)

    async def close(self):
        with self._clients_lock:
            clients = list(self._clients.values())
            self._clients.clear()
        for client in clients:
            try:
                await client.aclose()
            except Exception:
                pass


class DeepSeekProvider(BaseProvider):
    name = "deepseek"

    async def search(self, query, count=10, need_content=False, ori_question=None,
                     localization="", **_ignored):
        body = {"query": query, "count": count, "need_content": bool(need_content)}
        if ori_question:
            body["ori_question"] = ori_question
        if localization:
            body["localization"] = localization
        data = await self._request("/v1/tools/search", body)
        results = data.get("results") or []
        return {"provider": self.name, "results": results}

    async def fetch(self, url):
        data = await self._request("/v1/tools/fetch", {"url": url})
        return {"provider": self.name, **data}


class TavilyProvider(BaseProvider):
    name = "tavily"

    SEARCH_OPTS = ("search_depth", "topic", "time_range", "days", "include_answer",
                   "include_images", "include_domains", "exclude_domains", "country",
                   "chunks_per_source")

    async def search(self, query, count=10, need_content=False, **opts):
        body = {
            "query": query,
            "max_results": max(1, min(int(count), 20)),
            "include_raw_content": bool(need_content),
        }
        for key in self.SEARCH_OPTS:
            value = opts.get(key)
            if value not in (None, "", []):
                body[key] = value
        data = await self._request("/search", body)
        results = []
        for item in data.get("results") or []:
            results.append({
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "snippet": item.get("content", ""),
                "content": item.get("raw_content") or "",
                "site_name": "",
                "site_icon": item.get("favicon", ""),
                "date": item.get("published_date", ""),
                "rerank_score": item.get("score", 0),
                "authority": "",
            })
        out = {"provider": self.name, "results": results}
        if data.get("answer"):
            out["answer"] = data["answer"]
        return out

    async def extract(self, url):
        """对应 DeepSeek 的 fetch：抓取单页正文（Markdown）。独立计费。"""
        data = await self._request("/extract", {
            "urls": [url], "format": "markdown", "extract_depth": "basic",
        })
        results = data.get("results") or []
        if not results:
            failed = data.get("failed_results") or []
            reason = failed[0].get("error") if failed else "未提取到内容"
            raise ApiError(f"Tavily 抓取失败: {reason}")
        item = results[0]
        return {"provider": self.name, "title": item.get("url") or url,
                "content": item.get("raw_content") or ""}

    async def crawl(self, url, max_depth=1, max_breadth=10, limit=10, instructions=None):
        body = {
            "url": url,
            "max_depth": max(1, min(int(max_depth), 5)),
            "max_breadth": max(1, min(int(max_breadth), 100)),
            "limit": max(1, min(int(limit), 100)),
            "extract_depth": "basic",
            "format": "markdown",
        }
        if instructions:
            body["instructions"] = instructions
        data = await self._request("/crawl", body, timeout=max(self.timeout, 180))
        results = [{"url": r.get("url", ""), "content": r.get("raw_content") or ""}
                   for r in data.get("results") or []]
        return {"provider": self.name, "base_url": data.get("base_url", ""), "results": results}

    async def research(self, query, model="auto", output_length="standard", timeout=600.0):
        data = await self._request("/research", {
            "input": query, "model": model, "output_length": output_length,
        }, timeout=60)
        if data.get("status") == "completed" or data.get("content"):
            return {"provider": self.name, **data}
        request_id = data.get("request_id")
        if not request_id:
            raise ApiError(f"research 未返回 request_id: {str(data)[:200]}")
        key = self.pool.acquire()
        url = f"{self.base_url}/research/{request_id}"
        client = self._client(url)
        deadline = time.monotonic() + float(timeout)
        while time.monotonic() < deadline:
            await asyncio.sleep(5)
            resp = await client.get(url, headers=self._headers(key), timeout=30)
            if resp.status_code >= 400:
                raise self._classify(resp)
            data = resp.json()
            status = data.get("status")
            if status == "completed" or data.get("content"):
                return {"provider": self.name, **data}
            if status in ("failed", "error"):
                raise ApiError(f"research 任务失败: {str(data)[:300]}")
        raise ApiError(f"research 任务超时（>{int(timeout)}s），request_id={request_id}，可稍后重试")


PROVIDERS = {"deepseek": DeepSeekProvider, "tavily": TavilyProvider}
PROVIDER_NAMES = {"deepseek": "DeepSeek", "tavily": "Tavily"}


class SearchRouter:
    """跨平台负载均衡：主备 / 轮询 / 随机。"""

    def __init__(self, cfg: dict, stats=None):
        self.cfg = cfg
        self.stats = stats
        request_cfg = {**cfg.get("request", {}), "key_strategy": cfg.get("key_strategy", "round_robin")}
        router = ProxyRouter(cfg.get("proxy", {}))
        lb = cfg.get("load_balance", {})
        self.strategy = lb.get("strategy", "failover")
        order = lb.get("order") or list(PROVIDERS)
        self._providers = []
        platforms = cfg.get("platforms", {})
        for name in order + [n for n in PROVIDERS if n not in order]:
            plat = platforms.get(name)
            if not plat or not plat.get("enabled") or not plat.get("keys"):
                continue
            cls = PROVIDERS.get(name)
            if cls:
                self._providers.append(cls(plat, request_cfg, router))
        self._rr = itertools.count()
        self._lock = threading.Lock()

    @property
    def provider_names(self):
        return [p.name for p in self._providers]

    def _pick_order(self):
        if not self._providers:
            raise ApiError("没有可用的搜索平台（请在设置中启用平台并配置 API Key）")
        if self.strategy == "round_robin":
            with self._lock:
                n = next(self._rr)
            providers = self._providers
            return [providers[(n + i) % len(providers)] for i in range(len(providers))]
        if self.strategy == "random":
            providers = list(self._providers)
            random.shuffle(providers)
            return providers
        return list(self._providers)  # failover: 按优先级顺序

    async def search(self, query, count=10, need_content=False, ori_question=None,
                     localization="", tavily_opts=None):
        extra = tavily_opts or {}
        last_error = None
        for provider in self._pick_order():
            try:
                result = await provider.search(query, count=count, need_content=need_content,
                                               ori_question=ori_question, localization=localization,
                                               **extra)
            except ApiError as exc:
                last_error = exc
                logger.warning("平台 %s 搜索失败: %s", provider.name, exc)
                if self.stats:
                    self.stats.record(provider.name, "search", False)
                continue
            if self.stats:
                self.stats.record(provider.name, "search", True)
            logger.info("搜索命中平台 %s，%d 条结果", provider.name, len(result["results"]))
            return result
        raise last_error or ApiError("所有搜索平台均不可用")

    async def fetch(self, url):
        """DeepSeek fetch 优先，失败后降级 Tavily extract（按平台优先级顺序）。"""
        last_error = None
        for provider in self._pick_order():
            try:
                if provider.name == "deepseek":
                    result = await provider.fetch(url)
                elif provider.name == "tavily":
                    result = await provider.extract(url)
                else:
                    continue
            except ApiError as exc:
                last_error = exc
                logger.warning("平台 %s 抓取失败: %s", provider.name, exc)
                if self.stats:
                    self.stats.record(provider.name, "fetch", False)
                continue
            if self.stats:
                self.stats.record(provider.name, "fetch", True)
            return result
        raise last_error or ApiError("抓取功能需要启用至少一个平台并配置 API Key")

    def _tavily(self):
        for provider in self._providers:
            if provider.name == "tavily":
                return provider
        raise ApiError("该功能由 Tavily 提供，请启用 Tavily 平台并配置 API Key")

    async def crawl(self, url, max_depth=1, max_breadth=10, limit=10, instructions=None):
        provider = self._tavily()
        try:
            result = await provider.crawl(url, max_depth, max_breadth, limit, instructions)
        except ApiError:
            if self.stats:
                self.stats.record("tavily", "crawl", False)
            raise
        if self.stats:
            self.stats.record("tavily", "crawl", True)
        return result

    async def research(self, query, model="auto", output_length="standard", timeout=600.0):
        provider = self._tavily()
        try:
            result = await provider.research(query, model, output_length, timeout)
        except ApiError:
            if self.stats:
                self.stats.record("tavily", "research", False)
            raise
        if self.stats:
            self.stats.record("tavily", "research", True)
        return result

    async def close(self):
        for provider in self._providers:
            await provider.close()
