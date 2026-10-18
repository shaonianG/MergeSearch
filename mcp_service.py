"""MCP 服务：FastMCP streamable-http，运行在后台线程，可随时启停/随配置重启。"""
import asyncio
import json
import logging
import threading

import uvicorn
from mcp.server.fastmcp import FastMCP

from providers import ApiError, SearchRouter
from stats import StatsManager

logger = logging.getLogger("mcp_service")


class McpService:
    def __init__(self, config_manager, on_state_change=None):
        self._cm = config_manager
        self._on_state_change = on_state_change or (lambda running, msg="": None)
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._router: SearchRouter | None = None
        self._lock = threading.Lock()
        self.last_error = ""
        self.stats = StatsManager()

    @property
    def is_running(self) -> bool:
        return self._server is not None and self._server.started

    def endpoint(self) -> str:
        cfg = self._cm.config["server"]
        return f"http://{cfg['host']}:{cfg['port']}/mcp"

    def start(self) -> tuple[bool, str]:
        with self._lock:
            if self.is_running:
                return True, "服务已在运行"
            self.last_error = ""
            self._thread = threading.Thread(target=self._run, name="mcp-server", daemon=True)
            self._thread.start()
        # 等待启动结果
        started = threading.Event()
        self._wait_started = started
        started.wait(timeout=15)
        self._wait_started = None
        if self.is_running:
            self._on_state_change(True)
            return True, f"服务已启动: {self.endpoint()}"
        return False, self.last_error or "服务启动失败"

    def stop(self) -> tuple[bool, str]:
        with self._lock:
            server, loop, router = self._server, self._loop, self._router
        if server is None:
            return True, "服务未在运行"
        if loop and loop.is_running():
            loop.call_soon_threadsafe(self._shutdown, server, router)
        if self._thread:
            self._thread.join(timeout=10)
        with self._lock:
            self._server = self._loop = self._router = self._thread = None
        self._on_state_change(False)
        return True, "服务已停止"

    def restart(self) -> tuple[bool, str]:
        self.stop()
        return self.start()

    def _shutdown(self, server, router):
        server.should_exit = True
        if router:
            asyncio.ensure_future(router.close())

    def _run(self):
        cfg = self._cm.config
        server_cfg = cfg["server"]
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._router = SearchRouter(cfg, self.stats)
            app = self._build_app(cfg)
            config = uvicorn.Config(
                app, host=server_cfg["host"], port=int(server_cfg["port"]),
                log_level="warning", access_log=False,
                log_config=None,  # windowed exe 无控制台(sys.stdout=None)，禁用 uvicorn 自带日志配置
            )
            self._server = uvicorn.Server(config)
            # 通知主线程启动结果
            import time as _time
            def _notify():
                while not self._server.started:
                    if self._server.should_exit:
                        return
                    _time.sleep(0.05)
                cb = getattr(self, "_wait_started", None)
                if cb:
                    cb.set()
            threading.Thread(target=_notify, daemon=True).start()
            self._loop.run_until_complete(self._server.serve())
        except Exception as e:
            self.last_error = f"启动失败: {e}"
            logger.exception("MCP 服务异常")
        finally:
            cb = getattr(self, "_wait_started", None)
            if cb:
                cb.set()
            self._on_state_change(False)
            self._loop.close()

    def _build_app(self, cfg: dict):
        defaults = cfg.get("defaults", {})
        router = self._router
        stats = self.stats

        mcp = FastMCP(
            "merge-search",
            instructions=(
                "多平台联网搜索与网页抓取服务（Tavily 优先，DeepSeek 兜底）。"
                "search 在已启用平台间按负载均衡策略调用，Tavily 专属参数（topic/time_range 等）路由到 Tavily 时生效；"
                "fetch 抓取单个网页正文（Markdown，DeepSeek 失败时自动降级 Tavily extract）；"
                "crawl 整站爬取、research 深度研究（均为 Tavily 提供）。"
            ),
            streamable_http_path="/mcp",
        )

        @mcp.tool()
        async def search(
            query: str,
            count: int = 0,
            need_content: bool | None = None,
            ori_question: str | None = None,
            localization: str = "",
            search_depth: str = "",
            topic: str = "",
            time_range: str = "",
            days: int = 0,
            include_answer: bool = False,
            include_domains: list[str] | None = None,
            exclude_domains: list[str] | None = None,
            country: str = "",
        ) -> str:
            """联网搜索网页，返回结构化结果（标题/链接/摘要/可选正文），自动在 Tavily / DeepSeek 间负载均衡。

            Args:
                query: 搜索词（1~128 字符）。支持语法: site:example.com 限定站点、
                    -site: 排除站点、"精确短语"、lang:zh,en 限定语言、
                    after:2025-01-01 / before: 限定日期。
                count: 返回条数 1~50，默认 10。
                need_content: 是否返回网页正文（默认 false，开启后 content 字段有正文）。
                ori_question: 触发搜索的用户原始问题，有助于理解搜索意图。
                localization: 用于本地化结果的 IP 地址（可选，仅 DeepSeek 支持）。
                search_depth: 搜索深度 basic / advanced（仅 Tavily，advanced 更深但双倍计费）。
                topic: 垂直频道 general / news / finance（仅 Tavily）。
                time_range: 时间范围 day / week / month / year（仅 Tavily）。
                days: 最近 N 天（仅 Tavily，topic=news 时常用）。
                include_answer: 让 Tavily 额外生成一段 LLM 摘要答案（仅 Tavily）。
                include_domains: 域名白名单，最多 20 个（仅 Tavily）。
                exclude_domains: 域名黑名单，最多 20 个（仅 Tavily）。
                country: 国家代码如 cn / us，定向搜索结果（仅 Tavily，topic=general）。
            """
            count = count if 1 <= count <= 50 else int(defaults.get("search_count", 10))
            if need_content is None:
                need_content = bool(defaults.get("need_content", False))
            tavily_opts = {}
            if search_depth in ("basic", "advanced"):
                tavily_opts["search_depth"] = search_depth
            if topic in ("general", "news", "finance"):
                tavily_opts["topic"] = topic
            if time_range in ("day", "week", "month", "year"):
                tavily_opts["time_range"] = time_range
            if days > 0:
                tavily_opts["days"] = days
            if include_answer:
                tavily_opts["include_answer"] = True
            if include_domains:
                tavily_opts["include_domains"] = include_domains[:20]
            if exclude_domains:
                tavily_opts["exclude_domains"] = exclude_domains[:20]
            if country:
                tavily_opts["country"] = country
            try:
                data = await router.search(query, count, need_content, ori_question,
                                           localization, tavily_opts=tavily_opts)
            except ApiError as e:
                return f"搜索失败: {e}"
            results = data.get("results", [])
            if not results and not data.get("answer"):
                return "未找到相关结果。"
            payload = {"provider": data.get("provider"), "results": results}
            if data.get("answer"):
                payload["answer"] = data["answer"]
            return json.dumps(payload, ensure_ascii=False, indent=2)

        @mcp.tool()
        async def fetch(url: str) -> str:
            """抓取单个网页，以 Markdown 返回页面正文（DeepSeek 优先，失败自动降级 Tavily extract）。

            Args:
                url: 要抓取的网页地址（HTTP/HTTPS，不能带用户名密码）。
            """
            try:
                data = await router.fetch(url)
            except ApiError as e:
                return f"抓取失败: {e}"
            title = data.get("title") or ""
            content = data.get("content") or ""
            return f"# {title}\n\n{content}" if title else content or "未获取到内容。"

        @mcp.tool()
        async def crawl(
            url: str,
            max_depth: int = 1,
            max_breadth: int = 10,
            limit: int = 10,
            instructions: str | None = None,
        ) -> str:
            """整站爬取：从入口 URL 递归抓取站内多个页面的正文（由 Tavily 提供，按页计费）。

            Args:
                url: 入口页面地址。
                max_depth: 递归深度 1~5，默认 1。
                max_breadth: 每层最多跟随的链接数 1~100，默认 10。
                limit: 最多抓取的页面总数 1~100，默认 10。
                instructions: 自然语言爬取指令（如"只要文档页"），Tavily 会据此筛选链接。
            """
            try:
                data = await router.crawl(url, max_depth, max_breadth, limit, instructions)
            except ApiError as e:
                return f"爬取失败: {e}"
            results = data.get("results", [])
            if not results:
                return "未抓取到页面。"
            parts = [f"爬取自 {data.get('base_url') or url}，共 {len(results)} 页："]
            for item in results:
                content = (item.get("content") or "")[:3000]
                parts.append(f"\n## {item.get('url')}\n\n{content}")
            return "\n".join(parts)

        @mcp.tool()
        async def research(
            query: str,
            model: str = "auto",
            output_length: str = "standard",
            timeout: int = 600,
        ) -> str:
            """深度研究：多轮检索与分析后生成带引用的研究报告（由 Tavily 提供，耗时数十秒到数分钟）。

            Args:
                query: 研究课题或问题。
                model: 研究模型 mini（快、省，适合明确的小问题）/ pro（全面深入）/ auto（自动选择）。
                output_length: 报告长度 short / standard / long。
                timeout: 最长等待秒数，默认 600。
            """
            if model not in ("mini", "pro", "auto"):
                model = "auto"
            if output_length not in ("short", "standard", "long"):
                output_length = "standard"
            try:
                data = await router.research(query, model=model,
                                             output_length=output_length, timeout=float(timeout))
            except ApiError as e:
                return f"研究失败: {e}"
            content = data.get("content") or data.get("report") or ""
            sources = data.get("sources") or []
            parts = [content] if content else ["未获取到报告内容。"]
            if sources:
                parts.append("\n\n## 引用来源")
                for i, s in enumerate(sources, 1):
                    if isinstance(s, dict):
                        parts.append(f"{i}. [{s.get('title') or s.get('url')}]({s.get('url')})")
                    else:
                        parts.append(f"{i}. {s}")
            return "\n".join(parts)

        return mcp.streamable_http_app()
