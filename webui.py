"""Web 风格 GUI：pywebview 内嵌 web/index.html + JS 桥接 + 系统托盘。"""
import asyncio
import json
import logging
import os
import queue
import sys
import threading

from app_config import APP_VERSION, ConfigManager, app_dir, set_windows_autostart
from mcp_service import McpService
from providers import PROVIDERS, ApiError, SearchRouter

logger = logging.getLogger("webui")


class QueueLogHandler(logging.Handler):
    def __init__(self, q: queue.Queue):
        super().__init__()
        self.q = q

    def emit(self, record):
        try:
            self.q.put(self.format(record))
        except Exception:
            pass


def make_tray_image(color: str = "#2563eb"):
    from PIL import Image, ImageDraw

    candidates = [
        os.path.join(app_dir(), "assets", "icon.png"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "icon.png"),
    ]
    for p in candidates:
        if os.path.exists(p):
            try:
                return Image.open(p).convert("RGBA").resize((64, 64), Image.LANCZOS)
            except Exception:
                pass
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((8, 8, 40, 40), fill=color)
    d.ellipse((15, 15, 33, 33), fill=(255, 255, 255, 255))
    d.line((36, 36, 56, 56), fill=color, width=9)
    return img


class JsApi:
    """暴露给前端 JS 调用的接口（pywebview js_api）。"""

    def __init__(self, app: "WebApp"):
        self._app = app
        self._dl_thread = None
        self._dl_progress = None

    def get_config(self):
        return self._app.cm.config

    def save_config(self, cfg):
        try:
            old_autostart = self._app.cm.config.get("autostart", False)
            if cfg.get("autostart") != old_autostart:
                set_windows_autostart(bool(cfg.get("autostart")))
            # 配置未变化时跳过落盘和服务重启（加载配置回写等场景）
            if cfg == self._app.cm.config:
                return {"ok": True, "msg": "配置无变化"}
            self._app.cm.save(cfg)
            level = getattr(logging, cfg.get("log_level", "INFO"), logging.INFO)
            logging.getLogger().setLevel(level)
            if self._app.service.is_running:
                ok, msg = self._app.service.restart()
                return {"ok": ok, "msg": f"已保存，服务已重启: {msg}" if ok else msg}
            return {"ok": True, "msg": "配置已保存"}
        except Exception as e:
            return {"ok": False, "msg": f"保存失败: {e}"}

    def get_status(self):
        return {"running": self._app.service.is_running, "endpoint": self._app.service.endpoint(),
                "version": APP_VERSION}

    def start_service(self):
        ok, msg = self._app.service.start()
        logger.info(msg)
        return {"ok": ok, "msg": msg}

    def stop_service(self):
        ok, msg = self._app.service.stop()
        logger.info(msg)
        return {"ok": ok, "msg": msg}

    def test_search(self, query, count=5):
        async def go():
            router = SearchRouter(self._app.cm.config)
            try:
                data = await router.search(query, count=max(1, min(50, int(count))))
                return {"ok": True, "provider": data.get("provider"), "data": data.get("results", [])}
            except Exception as e:
                return {"ok": False, "msg": str(e)}
            finally:
                await router.close()

        return asyncio.run(go())

    def test_fetch(self, url):
        async def go():
            router = SearchRouter(self._app.cm.config)
            try:
                data = await router.fetch(url)
                return {"ok": True, "data": data}
            except Exception as e:
                return {"ok": False, "msg": str(e)}
            finally:
                await router.close()

        return asyncio.run(go())

    def _make_single_key_client(self, platform, key):
        cfg = self._app.cm.config
        plat = cfg.get("platforms", {}).get(platform)
        cls = PROVIDERS.get(platform)
        if not plat or not cls:
            raise ApiError(f"未知平台: {platform}")
        from proxy_router import ProxyRouter
        plat_cfg = {"base_url": plat.get("base_url", ""), "keys": [key]}
        request_cfg = {**cfg.get("request", {}), "key_strategy": "failover"}
        return cls(plat_cfg, request_cfg, ProxyRouter(cfg.get("proxy", {})))

    def test_key(self, platform, key):
        """测试单个 key 的连通性，返回 '可用' 或错误信息。"""
        async def go():
            client = self._make_single_key_client(platform, key)
            try:
                await client.search("DeepSeek", count=1)
                return "可用"
            except Exception as e:
                return str(e)
            finally:
                await client.close()

        try:
            return asyncio.run(go())
        except Exception as e:
            return str(e)

    def test_keys(self, platform, keys):
        return [{"key": k, "msg": self.test_key(platform, k)} for k in keys]

    def get_stats(self):
        return self._app.service.stats.snapshot()

    def reset_stats(self):
        self._app.service.stats.reset()
        return {"ok": True}

    def check_update(self):
        """检查 GitHub 最新 release。"""
        import updater
        try:
            return updater.run_check_and_download(self._app.cm.config.get("proxy", {}))
        except Exception as e:
            return {"has_update": False, "error": str(e)}

    def download_update(self, url):
        """后台线程下载新版 exe，立即返回。进度用 download_progress 轮询。"""
        import threading
        import updater
        if self._dl_thread and self._dl_thread.is_alive():
            return {"ok": False, "msg": "已有下载任务在进行中"}
        self._dl_progress = {"downloading": True, "done": 0, "total": 0, "path": None, "error": None}

        def worker():
            try:
                path = updater.download_update_sync(url, self._app.cm.config.get("proxy", {}),
                                                    lambda d, t: self._dl_progress.update(done=d, total=t))
                self._dl_progress.update(path=path, downloading=False)
            except Exception as e:
                self._dl_progress.update(error=str(e), downloading=False)

        self._dl_thread = threading.Thread(target=worker, daemon=True)
        self._dl_thread.start()
        return {"ok": True, "msg": "下载已开始"}

    def download_progress(self):
        """轮询下载进度。"""
        return self._dl_progress or {"downloading": False}

    def apply_update(self, path):
        """应用已下载的更新（替换 exe 并重启）。"""
        import updater
        result = updater.run_apply(path)
        if result.get("ok"):
            # 启动替换脚本后退出本进程，否则运行中的 exe 无法被覆盖
            import threading
            threading.Timer(1.0, self._app.quit).start()
        return result

    def get_app_version(self):
        return {"version": APP_VERSION}

    def get_logs(self):
        lines = []
        try:
            while True:
                lines.append(self._app.log_queue.get_nowait())
        except queue.Empty:
            pass
        return lines

    def hide_window(self):
        self._app.hide()

    def open_url(self, url):
        """用系统默认浏览器打开外部链接（官网控制台等）。"""
        import webbrowser
        url = str(url)
        if url.startswith(("http://", "https://")):
            webbrowser.open(url)

    def quit_app(self):
        self._app.quit()


class WebApp:
    def __init__(self):
        self.cm = ConfigManager()
        self.log_queue: queue.Queue = queue.Queue()
        handler = QueueLogHandler(self.log_queue)
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S"))
        root_logger = logging.getLogger()
        root_logger.addHandler(handler)
        root_logger.setLevel(getattr(logging, self.cm.config.get("log_level", "INFO"), logging.INFO))

        self.service = McpService(self.cm, on_state_change=self._on_service_state)
        self.service_running = False
        self.tray_icon = None
        self.window = None
        self._quitting = False

    def _on_service_state(self, running: bool, msg: str = ""):
        self.service_running = running
        if self.window:
            try:
                self.window.evaluate_js(f"window.onServiceState && window.onServiceState({json.dumps(running)})")
            except Exception:
                pass

    # ---------- 托盘 ----------
    def _setup_tray(self):
        import pystray

        def on_show(icon=None, item=None):
            self.show()

        def on_quit(icon=None, item=None):
            self.quit()

        menu = pystray.Menu(
            pystray.MenuItem("显示主窗口", on_show, default=True),
            pystray.MenuItem("退出", on_quit),
        )
        self.tray_icon = pystray.Icon("deepseek-search-mcp", make_tray_image(),
                                      "MergeSearch", menu)
        threading.Thread(target=self.tray_icon.run, daemon=True).start()

    def hide(self):
        if self.window:
            self.window.hide()

    def show(self):
        if self.window:
            self.window.show()
            self.window.restore()

    def quit(self):
        if self._quitting:
            return
        self._quitting = True
        try:
            self.service.stop()
        except Exception:
            pass
        if self.tray_icon:
            try:
                self.tray_icon.stop()
            except Exception:
                pass
        if self.window:
            self.window.destroy()

    def _on_closing(self):
        # 关闭窗口 = 最小化到托盘
        self.hide()
        return False

    def run(self):
        import webview

        html_path = os.path.join(app_dir(), "web", "index.html")
        if not os.path.exists(html_path):
            html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "index.html")
        self.window = webview.create_window(
            f"MergeSearch v{APP_VERSION}",
            html_path,
            js_api=JsApi(self),
            width=1100, height=720, min_size=(900, 600),
            text_select=True,
        )
        self.window.events.closing += self._on_closing
        self._setup_tray()
        if self.cm.config["server"].get("auto_start", True):
            threading.Timer(0.5, lambda: self.service.start() and None).start()
        webview.start(debug=False)


def run_app():
    WebApp().run()
