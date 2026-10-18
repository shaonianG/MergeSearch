"""端到端测试：分流规则、负载均衡路由、Tavily/DeepSeek 真实调用、MCP 服务。"""
import asyncio
import sys

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client as streamablehttp_client

from app_config import ConfigManager, migrate_config
from mcp_service import McpService
from providers import SearchRouter, TavilyProvider
from proxy_router import ProxyRouter


def test_proxy_router():
    r = ProxyRouter({
        "mode": "auto",
        "address": "http://127.0.0.1:7890",
        "rules": [
            "DOMAIN-SUFFIX,google.com,PROXY",
            "DOMAIN-KEYWORD,openai,PROXY",
            "DOMAIN-SUFFIX,deepseek.com,DIRECT",
            "DOMAIN,127.0.0.1,DIRECT",
            "IP-CIDR,10.0.0.0/8,DIRECT",
            "REGEX,^https?://.*\\.bing\\.,PROXY",
        ],
        "auto_default": "direct",
    })
    cases = [
        ("https://www.google.com/x", "proxy"),
        ("https://google.com/x", "proxy"),
        ("https://api.openai.com/v1", "proxy"),
        ("https://api.deepseek.com/v1/tools/search", "direct"),
        ("https://example.com/", "direct"),
        ("http://127.0.0.1:8765/mcp", "direct"),
        ("http://10.1.2.3/x", "direct"),
        ("https://cn.bing.com/x", "proxy"),
    ]
    for url, expect in cases:
        got = r.decide(url)
        assert got == expect, f"{url}: 期望 {expect} 实际 {got}"
    assert ProxyRouter({"mode": "global", "address": "http://x:1"}).decide("https://a.com") == "proxy"
    assert ProxyRouter({"mode": "direct", "address": "http://x:1"}).decide("https://a.com") == "direct"
    # 旧配置兼容
    legacy = ProxyRouter({"mode": "auto", "address": "http://x:1", "rules": ["*.google.com", "re:^https://x\\."]})
    assert legacy.decide("https://a.google.com/") == "proxy"
    print("proxy_router: OK")


def test_migrate():
    old = {
        "api_keys": ["sk-a", "sk-b"],
        "base_url": "https://api.deepseek.com",
        "proxy": {"proxy_rules": ["*.google.com", "re:^https://x\\."], "direct_rules": ["localhost", "*.cn"]},
    }
    new = migrate_config(old)
    ds = new["platforms"]["deepseek"]
    assert ds["keys"] == ["sk-a", "sk-b"] and ds["base_url"] == "https://api.deepseek.com"
    rules = new["proxy"]["rules"]
    assert "DOMAIN-SUFFIX,google.com,PROXY" in rules and "REGEX,^https://x\\.,PROXY" in rules
    assert "DOMAIN,localhost,DIRECT" in rules and "DOMAIN-SUFFIX,cn,DIRECT" in rules
    print("migrate_config: OK")


async def test_providers():
    cfg = ConfigManager().config
    # Tavily 直连真实调用
    tv_cfg = dict(cfg)
    tv = SearchRouter(tv_cfg)
    try:
        # 单独测 Tavily（把它排到第一位）
        tv_cfg2 = dict(cfg)
        tv_cfg2["load_balance"] = {"strategy": "failover", "order": ["tavily", "deepseek"]}
        r2 = SearchRouter(tv_cfg2)
        data = await r2.search("DeepSeek V4", count=3)
        print("tavily 命中平台:", data["provider"], "结果数:", len(data["results"]))
        assert data["provider"] == "tavily", data["provider"]
        assert data["results"], "tavily 无结果"
        await r2.close()

        # failover：坏 key 平台在前，应落到 deepseek
        import copy
        bad = copy.deepcopy(cfg)
        bad["platforms"]["tavily"] = {"enabled": True, "keys": ["tvly-bad-key"], "base_url": "https://api.tavily.com"}
        bad["load_balance"] = {"strategy": "failover", "order": ["tavily", "deepseek"]}
        r3 = SearchRouter(bad)
        data = await r3.search("DeepSeek V4", count=2)
        print("坏key故障转移命中:", data["provider"])
        assert data["provider"] == "deepseek"
        await r3.close()
    finally:
        await tv.close()
    print("providers: OK")


async def test_new_features():
    """extract 降级、crawl、research、Tavily 高级搜索参数。"""
    import copy
    cfg = ConfigManager().config

    # 1) Tavily 高级参数：news 频道 + 时间范围 + LLM 摘要答案
    r = SearchRouter(cfg)
    try:
        data = await r.search("artificial intelligence", count=3, tavily_opts={
            "topic": "news", "days": 7, "include_answer": True, "search_depth": "basic",
        })
        print("高级参数搜索命中:", data["provider"], "结果数:", len(data["results"]),
              "answer:", bool(data.get("answer")))
        assert data["provider"] == "tavily" and data["results"]

        # 2) fetch：tavily 在前 → extract 提供服务
        data = await r.fetch("https://example.com")
        print("fetch 命中平台:", data["provider"], "正文字数:", len(data.get("content") or ""))
        assert data["provider"] == "tavily" and data.get("content")

        # 3) fetch 降级：tavily 坏 key → deepseek fetch 兜底
        bad = copy.deepcopy(cfg)
        bad["platforms"]["tavily"] = {"enabled": True, "keys": ["tvly-bad-key"],
                                      "base_url": "https://api.tavily.com"}
        r2 = SearchRouter(bad)
        data = await r2.fetch("https://example.com")
        print("fetch 降级命中平台:", data["provider"])
        assert data["provider"] == "deepseek" and data.get("content")
        await r2.close()

        # 4) crawl：小范围爬取（example.com 无外链，换有链接的真实站点）
        data = await r.crawl("https://docs.tavily.com/welcome", max_depth=1, max_breadth=3, limit=3)
        print("crawl 页数:", len(data["results"]), "base:", data.get("base_url"))
        assert data["results"] and data["results"][0].get("content") is not None

        # 5) research：mini + short（异步轮询）
        if "--skip-research" not in sys.argv:
            data = await r.research("What is the Model Context Protocol (MCP)?",
                                    model="mini", output_length="short", timeout=300)
            content = data.get("content") or ""
            print("research 状态:", data.get("status"), "报告字数:", len(content))
            assert content, f"research 无内容: {str(data)[:200]}"
    finally:
        await r.close()
    print("new_features: OK")


async def test_mcp():
    cm = ConfigManager()
    service = McpService(cm)
    ok, msg = service.start()
    print("start:", ok, msg)
    assert ok, msg
    try:
        url = service.endpoint()
        async with streamablehttp_client(url) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                names = [t.name for t in tools.tools]
                print("tools:", names)
                assert set(names) == {"search", "fetch", "crawl", "research"}, names
                result = await session.call_tool("search", {"query": "DeepSeek V4 模型 发布", "count": 3})
                text = result.content[0].text
                print("search result 前300字:", text[:300])
                assert "失败" not in text[:20], text[:200]
        import json
        stats = service.stats.snapshot()
        print("stats:", json.dumps(stats["counters"], ensure_ascii=False))
    finally:
        service.stop()
        print("stopped")


if __name__ == "__main__":
    test_proxy_router()
    test_migrate()
    if "--no-api" not in sys.argv:
        asyncio.run(test_providers())
        asyncio.run(test_new_features())
        asyncio.run(test_mcp())
