"""GUI：主窗口（状态/日志/测试）、设置对话框、系统托盘。基于 ttkbootstrap 主题。"""
import asyncio
import copy
import logging
import queue
import threading
import tkinter as tk
from tkinter import messagebox

import ttkbootstrap as ttk

from app_config import ConfigManager, set_windows_autostart
from providers import DeepSeekProvider


def _make_ds_client(cfg, keys=None):
    """从完整配置构造 DeepSeek 客户端（兼容层）。"""
    from proxy_router import ProxyRouter
    plat = dict(cfg.get("platforms", {}).get("deepseek", {}))
    if keys is not None:
        plat["keys"] = keys
    request_cfg = {**cfg.get("request", {}), "key_strategy": cfg.get("key_strategy", "round_robin")}
    return DeepSeekProvider(plat, request_cfg, ProxyRouter(cfg.get("proxy", {})))
from mcp_service import McpService

logger = logging.getLogger("gui")


class QueueLogHandler(logging.Handler):
    def __init__(self, q: queue.Queue):
        super().__init__()
        self.q = q

    def emit(self, record):
        try:
            self.q.put(self.format(record))
        except Exception:
            pass


def make_tray_image(color: str = "#1e88e5"):
    """用 Pillow 画一个放大镜托盘图标。"""
    from PIL import Image, ImageDraw

    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((8, 8, 40, 40), fill=color)
    d.ellipse((15, 15, 33, 33), fill=(255, 255, 255, 255))
    d.line((36, 36, 56, 56), fill=color, width=9)
    return img


def theme_plain_widget(style: ttk.Style, widget: tk.Text | tk.Listbox):
    """让 tk 原生 Text/Listbox 颜色跟随当前主题。"""
    c = style.colors
    widget.configure(
        bg=getattr(c, "inputbg", c.bg), fg=getattr(c, "inputfg", c.fg),
        selectbackground=getattr(c, "selectbg", c.primary),
        selectforeground=getattr(c, "selectfg", "#ffffff"),
        relief="flat", highlightthickness=1,
        highlightbackground=getattr(c, "border", c.bg),
    )
    if isinstance(widget, tk.Text):  # Listbox 不支持 insertbackground
        widget.configure(insertbackground=getattr(c, "inputfg", c.fg))


class LogPanel(ttk.Frame):
    def __init__(self, master, style: ttk.Style):
        super().__init__(master)
        self.text = tk.Text(self, height=10, state="disabled", wrap="word",
                            font=("Consolas", 9))
        theme_plain_widget(style, self.text)
        scroll = ttk.Scrollbar(self, command=self.text.yview)
        self.text.configure(yscrollcommand=scroll.set)
        self.text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

    def append(self, line: str):
        self.text.configure(state="normal")
        self.text.insert("end", line + "\n")
        self.text.see("end")
        lines = int(self.text.index("end-1c").split(".")[0])
        if lines > 500:
            self.text.delete("1.0", "100.0")
        self.text.configure(state="disabled")

    def clear(self):
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")


class App:
    def __init__(self):
        self.cm = ConfigManager()
        self.log_queue: queue.Queue = queue.Queue()
        handler = QueueLogHandler(self.log_queue)
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", "%H:%M:%S"))
        root_logger = logging.getLogger()
        root_logger.addHandler(handler)
        root_logger.setLevel(getattr(logging, self.cm.config.get("log_level", "INFO"), logging.INFO))

        self.service = McpService(self.cm, on_state_change=self._on_service_state)
        self.tray_icon = None
        self._quitting = False

        self._build_window()
        self._setup_tray()
        self.root.after(100, self._poll_log_queue)
        if self.cm.config["server"].get("auto_start", True):
            self.root.after(300, self.start_service)

    # ---------- 窗口 ----------
    def _build_window(self):
        theme = self.cm.config.get("ui_theme", "bootstrap-dark")
        self.root = ttk.Window(themename=theme)
        self.style = ttk.Style()
        self.root.title("MergeSearch")
        self.root.geometry("660x480")
        self.root.minsize(560, 400)
        self.root.protocol("WM_DELETE_WINDOW", self.hide_to_tray)

        top = ttk.LabelFrame(self.root, text="服务状态", padding=12, bootstyle="primary")
        top.pack(fill="x", padx=12, pady=(12, 6))

        self.status_var = tk.StringVar(value="● 未运行")
        self.status_label = ttk.Label(top, textvariable=self.status_var,
                                      font=("", 11, "bold"), bootstyle="secondary")
        self.status_label.pack(side="left")

        self.endpoint_var = tk.StringVar(value=self.service.endpoint())
        endpoint_entry = ttk.Entry(top, textvariable=self.endpoint_var, state="readonly", width=38)
        endpoint_entry.pack(side="left", padx=12)
        ttk.Button(top, text="复制地址", command=self.copy_endpoint,
                   bootstyle="info-outline").pack(side="left")

        btns = ttk.Frame(self.root, padding=(12, 6))
        btns.pack(fill="x")
        self.toggle_btn = ttk.Button(btns, text="启动服务", command=self.toggle_service,
                                     bootstyle="success")
        self.toggle_btn.pack(side="left")
        ttk.Button(btns, text="设置", command=self.open_settings,
                   bootstyle="secondary").pack(side="left", padx=6)
        ttk.Button(btns, text="测试搜索", command=self.open_test_dialog,
                   bootstyle="info").pack(side="left")
        ttk.Button(btns, text="清空日志", command=lambda: self.log_panel.clear(),
                   bootstyle="warning-outline").pack(side="left", padx=6)
        ttk.Button(btns, text="最小化到托盘", command=self.hide_to_tray,
                   bootstyle="link").pack(side="right")

        log_frame = ttk.LabelFrame(self.root, text="运行日志", padding=6, bootstyle="primary")
        log_frame.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self.log_panel = LogPanel(log_frame, self.style)
        self.log_panel.pack(fill="both", expand=True)

    def _poll_log_queue(self):
        try:
            while True:
                self.log_panel.append(self.log_queue.get_nowait())
        except queue.Empty:
            pass
        if not self._quitting:
            self.root.after(150, self._poll_log_queue)

    # ---------- 托盘 ----------
    def _setup_tray(self):
        import pystray

        def on_show(icon=None, item=None):
            self.root.after(0, self.show_window)

        def on_toggle(icon=None, item=None):
            self.root.after(0, self.toggle_service)

        def on_quit(icon=None, item=None):
            self.root.after(0, self.quit_app)

        menu = pystray.Menu(
            pystray.MenuItem("显示主窗口", on_show, default=True),
            pystray.MenuItem("启动/停止服务", on_toggle),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", on_quit),
        )
        self.tray_icon = pystray.Icon("deepseek-search-mcp", make_tray_image(),
                                      "MergeSearch", menu)
        threading.Thread(target=self.tray_icon.run, daemon=True).start()

    def hide_to_tray(self):
        self.root.withdraw()

    def show_window(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def quit_app(self):
        if self._quitting:
            return
        self._quitting = True
        self.service.stop()
        if self.tray_icon:
            self.tray_icon.stop()
        self.root.destroy()

    # ---------- 服务控制 ----------
    def _on_service_state(self, running: bool, msg: str = ""):
        def update():
            if running:
                self.status_var.set("● 运行中")
                self.status_label.configure(bootstyle="success")
                self.toggle_btn.configure(text="停止服务", bootstyle="danger")
            else:
                self.status_var.set("● 未运行")
                self.status_label.configure(bootstyle="secondary")
                self.toggle_btn.configure(text="启动服务", bootstyle="success")
            self.endpoint_var.set(self.service.endpoint())
        try:
            self.root.after(0, update)
        except RuntimeError:
            pass

    def toggle_service(self):
        if self.service.is_running:
            ok, msg = self.service.stop()
        else:
            ok, msg = self._start_in_thread()
            return
        if not ok:
            messagebox.showerror("错误", msg)

    def _start_in_thread(self):
        self.toggle_btn.configure(state="disabled")

        def work():
            ok, msg = self.service.start()
            def done():
                self.toggle_btn.configure(state="normal")
                if not ok:
                    messagebox.showerror("启动失败", msg)
                logger.info(msg)
            self.root.after(0, done)

        threading.Thread(target=work, daemon=True).start()
        return True, ""

    def start_service(self):
        if not self.service.is_running:
            self._start_in_thread()

    def copy_endpoint(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.endpoint_var.get())

    # ---------- 设置 ----------
    def open_settings(self):
        SettingsDialog(self.root, self.cm, self.style, on_save=self._on_settings_saved)

    def _on_settings_saved(self):
        root_logger = logging.getLogger()
        root_logger.setLevel(getattr(logging, self.cm.config.get("log_level", "INFO"), logging.INFO))
        self.endpoint_var.set(self.service.endpoint())
        new_theme = self.cm.config.get("ui_theme", "bootstrap-dark")
        if new_theme != self.style.theme.name:
            self.apply_theme(new_theme)
        if self.service.is_running:
            def work():
                ok, msg = self.service.restart()
                self.root.after(0, lambda: logger.info("配置已保存，服务已重启: %s", msg))
                if not ok:
                    self.root.after(0, lambda: messagebox.showerror("重启失败", msg))
            threading.Thread(target=work, daemon=True).start()
        else:
            logger.info("配置已保存")

    def apply_theme(self, theme: str):
        """切换主题，并同步 tk 原生控件颜色。"""
        try:
            self.style.theme_use(theme)
        except tk.TclError:
            return
        theme_plain_widget(self.style, self.log_panel.text)

    # ---------- 测试 ----------
    def open_test_dialog(self):
        TestDialog(self.root, self.cm, self.style)

    def run(self):
        self.root.mainloop()


class SettingsDialog(ttk.Toplevel):
    STRATEGIES = {"round_robin": "轮询", "random": "随机", "failover": "主备（优先第一个）"}
    PROXY_MODES = {"direct": "直连", "auto": "自动（按规则）", "global": "全局代理"}

    def __init__(self, master, cm: ConfigManager, style: ttk.Style, on_save):
        super().__init__(master)
        self.cm = cm
        self._style = style
        self.on_save = on_save
        self.cfg = cm.config
        self.title("设置")
        self.geometry("600x560")
        self.transient(master)
        self.grab_set()

        nb = ttk.Notebook(self, bootstyle="primary")
        nb.pack(fill="both", expand=True, padx=12, pady=12)
        self._build_keys_tab(nb)
        self._build_proxy_tab(nb)
        self._build_general_tab(nb)

        bottom = ttk.Frame(self, padding=12)
        bottom.pack(fill="x")
        ttk.Button(bottom, text="保存", command=self.save, bootstyle="success").pack(side="right")
        ttk.Button(bottom, text="取消", command=self.destroy,
                   bootstyle="secondary-outline").pack(side="right", padx=6)

    # ----- 密钥页 -----
    def _build_keys_tab(self, nb):
        tab = ttk.Frame(nb, padding=10)
        nb.add(tab, text="  API 密钥  ")

        ttk.Label(tab, text="API Keys（可配置多个，失败自动切换）:").pack(anchor="w")
        mid = ttk.Frame(tab)
        mid.pack(fill="both", expand=True, pady=5)
        self.keys_list = tk.Listbox(mid, font=("Consolas", 9))
        theme_plain_widget(self._style, self.keys_list)
        scroll = ttk.Scrollbar(mid, command=self.keys_list.yview)
        self.keys_list.configure(yscrollcommand=scroll.set)
        self.keys_list.pack(side="left", fill="both", expand=True)
        scroll.pack(side="left", fill="y")
        self._real_keys = list(self.cfg.get("platforms", {}).get("deepseek", {}).get("keys", []))
        for k in self._real_keys:
            self.keys_list.insert("end", self._mask(k))

        kbtns = ttk.Frame(mid)
        kbtns.pack(side="left", fill="y", padx=6)
        ttk.Button(kbtns, text="添加", command=self._add_key, bootstyle="success-outline").pack(fill="x", pady=2)
        ttk.Button(kbtns, text="编辑", command=self._edit_key, bootstyle="info-outline").pack(fill="x", pady=2)
        ttk.Button(kbtns, text="删除", command=self._del_key, bootstyle="danger-outline").pack(fill="x", pady=2)
        ttk.Button(kbtns, text="上移", command=lambda: self._move_key(-1), bootstyle="secondary-outline").pack(fill="x", pady=2)
        ttk.Button(kbtns, text="下移", command=lambda: self._move_key(1), bootstyle="secondary-outline").pack(fill="x", pady=2)
        ttk.Button(kbtns, text="测试密钥", command=self._test_keys, bootstyle="warning").pack(fill="x", pady=(12, 2))

        row = ttk.Frame(tab)
        row.pack(fill="x", pady=4)
        ttk.Label(row, text="Key 切换策略:").pack(side="left")
        self.strategy_var = tk.StringVar(value=self.STRATEGIES.get(self.cfg.get("key_strategy"), "轮询"))
        ttk.Combobox(row, textvariable=self.strategy_var, state="readonly", width=18,
                     values=list(self.STRATEGIES.values())).pack(side="left", padx=5)

        row2 = ttk.Frame(tab)
        row2.pack(fill="x", pady=4)
        ttk.Label(row2, text="API Base URL:").pack(side="left")
        self.base_url_var = tk.StringVar(
            value=self.cfg.get("platforms", {}).get("deepseek", {}).get("base_url", "https://api.deepseek.com"))
        ttk.Entry(row2, textvariable=self.base_url_var, width=42).pack(side="left", padx=5)

    @staticmethod
    def _mask(key: str) -> str:
        return key if len(key) <= 10 else key[:6] + "..." + key[-4:]

    def _selected(self):
        sel = self.keys_list.curselection()
        return sel[0] if sel else None

    def _add_key(self):
        self._key_editor("添加 API Key", "")

    def _edit_key(self):
        i = self._selected()
        if i is not None:
            self._key_editor("编辑 API Key", self._real_keys[i], i)

    def _key_editor(self, title, value, index=None):
        dlg = ttk.Toplevel(self)
        dlg.title(title)
        dlg.transient(self)
        dlg.grab_set()
        ttk.Label(dlg, text="API Key:").pack(padx=10, pady=(10, 0))
        var = tk.StringVar(value=value)
        ttk.Entry(dlg, textvariable=var, width=52).pack(padx=10, pady=5)

        def ok():
            key = var.get().strip()
            if not key:
                return
            if index is None:
                self._real_keys.append(key)
                self.keys_list.insert("end", self._mask(key))
            else:
                self._real_keys[index] = key
                self.keys_list.delete(index)
                self.keys_list.insert(index, self._mask(key))
            dlg.destroy()

        ttk.Button(dlg, text="确定", command=ok, bootstyle="success").pack(pady=(0, 10))

    def _del_key(self):
        i = self._selected()
        if i is not None:
            self._real_keys.pop(i)
            self.keys_list.delete(i)

    def _move_key(self, delta: int):
        i = self._selected()
        j = None if i is None else i + delta
        if i is None or not (0 <= j < len(self._real_keys)):
            return
        self._real_keys[i], self._real_keys[j] = self._real_keys[j], self._real_keys[i]
        self.keys_list.delete(0, "end")
        for k in self._real_keys:
            self.keys_list.insert("end", self._mask(k))
        self.keys_list.selection_set(j)

    def _test_keys(self):
        if not self._real_keys:
            messagebox.showinfo("提示", "请先添加 API Key", parent=self)
            return

        async def test_one(key):
            cfg = copy.deepcopy(self.cfg)
            client = _make_ds_client(cfg, [key])
            try:
                await client.search("DeepSeek", count=1)
                return key, "✓ 可用"
            except Exception as e:
                return key, f"✗ {e}"
            finally:
                await client.close()

        def work():
            results = []
            for key in self._real_keys:
                results.append(asyncio.run(test_one(key)))
            def show():
                msg = "\n".join(f"{self._mask(k)}  {r}" for k, r in results)
                messagebox.showinfo("密钥测试结果", msg, parent=self)
            self.after(0, show)

        threading.Thread(target=work, daemon=True).start()

    # ----- 代理页 -----
    def _build_proxy_tab(self, nb):
        tab = ttk.Frame(nb, padding=10)
        nb.add(tab, text="  代理  ")
        p = self.cfg.get("proxy", {})

        row = ttk.Frame(tab)
        row.pack(fill="x", pady=4)
        ttk.Label(row, text="代理模式:").pack(side="left")
        self.proxy_mode_var = tk.StringVar(value=self.PROXY_MODES.get(p.get("mode"), "自动（按规则）"))
        ttk.Combobox(row, textvariable=self.proxy_mode_var, state="readonly", width=16,
                     values=list(self.PROXY_MODES.values())).pack(side="left", padx=5)

        row2 = ttk.Frame(tab)
        row2.pack(fill="x", pady=4)
        ttk.Label(row2, text="代理地址:").pack(side="left")
        self.proxy_addr_var = tk.StringVar(value=p.get("address", ""))
        ttk.Entry(row2, textvariable=self.proxy_addr_var, width=34).pack(side="left", padx=5)
        ttk.Label(row2, text="如 http://127.0.0.1:7890 或 socks5://...",
                  bootstyle="secondary").pack(side="left")

        panes = ttk.Frame(tab)
        panes.pack(fill="both", expand=True, pady=5)
        left = ttk.LabelFrame(panes, text="走代理的域名规则（自动模式）", padding=5, bootstyle="info")
        left.pack(side="left", fill="both", expand=True, padx=(0, 3))
        self.proxy_rules_text = tk.Text(left, height=8, font=("Consolas", 9))
        theme_plain_widget(self._style, self.proxy_rules_text)
        self.proxy_rules_text.pack(fill="both", expand=True)
        self.proxy_rules_text.insert("1.0", "\n".join(
            r for r in p.get("rules", []) if r.upper().endswith(",PROXY")))

        right = ttk.LabelFrame(panes, text="直连的域名规则（自动模式）", padding=5, bootstyle="success")
        right.pack(side="left", fill="both", expand=True, padx=(3, 0))
        self.direct_rules_text = tk.Text(right, height=8, font=("Consolas", 9))
        theme_plain_widget(self._style, self.direct_rules_text)
        self.direct_rules_text.pack(fill="both", expand=True)
        self.direct_rules_text.insert("1.0", "\n".join(
            r for r in p.get("rules", []) if r.upper().endswith(",DIRECT")))

        ttk.Label(tab, text="支持 *.example.com 通配符与 re: 开头的正则，每行一条；代理规则优先于直连规则。",
                  bootstyle="secondary").pack(anchor="w")
        row3 = ttk.Frame(tab)
        row3.pack(fill="x", pady=4)
        ttk.Label(row3, text="未命中规则时:").pack(side="left")
        self.auto_default_var = tk.StringVar(value="走代理" if p.get("auto_default") == "proxy" else "直连")
        ttk.Combobox(row3, textvariable=self.auto_default_var, state="readonly", width=8,
                     values=["直连", "走代理"]).pack(side="left", padx=5)

    # ----- 常规页 -----
    def _build_general_tab(self, nb):
        tab = ttk.Frame(nb, padding=10)
        nb.add(tab, text="  常规  ")
        s = self.cfg.get("server", {})
        r = self.cfg.get("request", {})
        d = self.cfg.get("defaults", {})

        def add_row(label, default, row):
            ttk.Label(row, text=label, width=22).pack(side="left")
            var = tk.StringVar(value=str(default))
            ttk.Entry(row, textvariable=var, width=20).pack(side="left")
            return var

        grid = ttk.Frame(tab)
        grid.pack(fill="x")
        fields = [
            ("监听地址", s.get("host", "127.0.0.1"), "host_var"),
            ("监听端口", s.get("port", 8765), "port_var"),
            ("请求超时（秒）", r.get("timeout", 30), "timeout_var"),
            ("失败重试次数", r.get("max_retries", 2), "retries_var"),
            ("默认搜索条数(1-50)", d.get("search_count", 10), "count_var"),
        ]
        for label, default, attr in fields:
            row = ttk.Frame(grid)
            row.pack(fill="x", pady=3)
            setattr(self, attr, add_row(label, default, row))

        row = ttk.Frame(tab)
        row.pack(fill="x", pady=3)
        ttk.Label(row, text="界面主题", width=22).pack(side="left")
        themes = sorted(self._style.theme_names())
        current = self.cfg.get("ui_theme", "bootstrap-dark")
        self.theme_var = tk.StringVar(value=current if current in themes else "bootstrap-dark")
        theme_cb = ttk.Combobox(row, textvariable=self.theme_var, state="readonly", width=20, values=themes)
        theme_cb.pack(side="left")
        theme_cb.bind("<<ComboboxSelected>>", lambda e: self._preview_theme())
        ttk.Label(row, text="（选择后即时预览）", bootstyle="secondary").pack(side="left", padx=6)

        self.need_content_var = tk.BooleanVar(value=bool(d.get("need_content", False)))
        ttk.Checkbutton(tab, text="搜索默认返回网页正文 (need_content)", variable=self.need_content_var,
                        bootstyle="round-toggle").pack(anchor="w", pady=3)
        self.auto_start_var = tk.BooleanVar(value=bool(s.get("auto_start", True)))
        ttk.Checkbutton(tab, text="启动程序后自动开启 MCP 服务", variable=self.auto_start_var,
                        bootstyle="round-toggle").pack(anchor="w", pady=3)
        self.autostart_var = tk.BooleanVar(value=bool(self.cfg.get("autostart", False)))
        ttk.Checkbutton(tab, text="开机自启（仅打包后的 exe 生效）", variable=self.autostart_var,
                        bootstyle="round-toggle").pack(anchor="w", pady=3)

        row = ttk.Frame(tab)
        row.pack(fill="x", pady=3)
        ttk.Label(row, text="日志级别", width=22).pack(side="left")
        self.log_level_var = tk.StringVar(value=self.cfg.get("log_level", "INFO"))
        ttk.Combobox(row, textvariable=self.log_level_var, state="readonly", width=18,
                     values=["DEBUG", "INFO", "WARNING", "ERROR"]).pack(side="left")

    def _preview_theme(self):
        """设置页选择主题时即时预览整个应用。"""
        app = self.master
        theme = self.theme_var.get()
        if hasattr(app, "apply_theme"):
            app.apply_theme(theme)
            theme_plain_widget(self._style, self.keys_list)
            theme_plain_widget(self._style, self.proxy_rules_text)
            theme_plain_widget(self._style, self.direct_rules_text)

    # ----- 保存 -----
    def save(self):
        cfg = self.cm.config
        ds = cfg.setdefault("platforms", {}).setdefault("deepseek", {})
        ds["keys"] = list(self._real_keys)
        strategy = {v: k for k, v in self.STRATEGIES.items()}.get(self.strategy_var.get(), "round_robin")
        cfg["key_strategy"] = strategy
        ds["base_url"] = self.base_url_var.get().strip() or "https://api.deepseek.com"

        mode = {v: k for k, v in self.PROXY_MODES.items()}.get(self.proxy_mode_var.get(), "auto")
        rules = [r if "," in r else f"DOMAIN-SUFFIX,{r.lstrip('*.')},PROXY"
                 for r in self._lines(self.proxy_rules_text)]
        rules += [r if "," in r else f"DOMAIN-SUFFIX,{r.lstrip('*.')},DIRECT"
                  for r in self._lines(self.direct_rules_text)]
        cfg["proxy"] = {
            "mode": mode,
            "address": self.proxy_addr_var.get().strip(),
            "rules": rules,
            "auto_default": "proxy" if self.auto_default_var.get() == "走代理" else "direct",
        }
        try:
            cfg["server"]["port"] = int(self.port_var.get())
            cfg["request"]["timeout"] = float(self.timeout_var.get())
            cfg["request"]["max_retries"] = int(self.retries_var.get())
            cfg["defaults"]["search_count"] = max(1, min(50, int(self.count_var.get())))
        except ValueError:
            messagebox.showerror("错误", "端口/超时/重试/条数必须是数字", parent=self)
            return
        cfg["server"]["host"] = self.host_var.get().strip() or "127.0.0.1"
        cfg["server"]["auto_start"] = self.auto_start_var.get()
        cfg["defaults"]["need_content"] = self.need_content_var.get()
        cfg["autostart"] = self.autostart_var.get()
        cfg["log_level"] = self.log_level_var.get()
        cfg["ui_theme"] = self.theme_var.get()

        if self.autostart_var.get() != self.cm.config.get("autostart", False):
            if not set_windows_autostart(self.autostart_var.get()):
                logger.info("开机自启仅在打包后的 exe 上生效")

        self.cm.save(cfg)
        self.on_save()
        self.destroy()

    @staticmethod
    def _lines(text: tk.Text) -> list[str]:
        return [ln.strip() for ln in text.get("1.0", "end").splitlines() if ln.strip()]


class TestDialog(ttk.Toplevel):
    def __init__(self, master, cm: ConfigManager, style: ttk.Style):
        super().__init__(master)
        self.cm = cm
        self.title("测试搜索")
        self.geometry("640x480")

        top = ttk.Frame(self, padding=10)
        top.pack(fill="x")
        ttk.Label(top, text="搜索词:").pack(side="left")
        self.query_var = tk.StringVar()
        entry = ttk.Entry(top, textvariable=self.query_var, width=40)
        entry.pack(side="left", padx=5)
        entry.bind("<Return>", lambda e: self.run_test())
        self.run_btn = ttk.Button(top, text="搜索", command=self.run_test, bootstyle="info")
        self.run_btn.pack(side="left")
        ttk.Button(top, text="测试 Fetch", command=self.run_fetch, bootstyle="secondary").pack(side="left", padx=5)

        self.output = tk.Text(self, wrap="word", font=("Consolas", 9))
        theme_plain_widget(style, self.output)
        scroll = ttk.Scrollbar(self, command=self.output.yview)
        self.output.configure(yscrollcommand=scroll.set)
        self.output.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        scroll.pack(side="right", fill="y")

    def _run_async(self, coro_factory):
        self.run_btn.configure(state="disabled")
        self.output.delete("1.0", "end")
        self.output.insert("end", "请求中...\n")

        def work():
            async def go():
                client = _make_ds_client(self.cm.config)
                try:
                    return await coro_factory(client)
                finally:
                    await client.close()
            try:
                result = asyncio.run(go())
            except Exception as e:
                result = f"错误: {e}"
            def done():
                self.output.delete("1.0", "end")
                self.output.insert("end", result)
                self.run_btn.configure(state="normal")
            self.after(0, done)

        threading.Thread(target=work, daemon=True).start()

    def run_test(self):
        query = self.query_var.get().strip()
        if not query:
            return
        import json

        async def call(client):
            data = await client.search(query, count=5)
            return json.dumps(data.get("results", []), ensure_ascii=False, indent=2)

        self._run_async(call)

    def run_fetch(self):
        url = self.query_var.get().strip()
        if not url.startswith("http"):
            messagebox.showinfo("提示", "请在输入框填写要抓取的 URL", parent=self)
            return

        async def call(client):
            data = await client.fetch(url)
            return f"# {data.get('title', '')}\n\n{data.get('content', '')}"

        self._run_async(call)


def run_app():
    App().run()
