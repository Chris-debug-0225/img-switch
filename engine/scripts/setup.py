#!/usr/bin/env python3
"""failover-imagegen-tool 配置管理：添加/删除/启停/排序中转源，支持命令行与图形界面。

命令行：
  python setup.py add --name example-source --base-url https://relay.example.com/v1 --api-key sk-xxx --model gpt-image-2.5-flare
  python setup.py list                       # 查看源与优先级顺序（密钥脱敏）
  python setup.py remove --name example-source
  python setup.py enable --name example-source        # disable 同理
  python setup.py up --name example-source            # 上移提升优先级，down 下移
  python setup.py path                       # 打印配置文件路径

图形界面：
  python setup.py gui

配置保存在 skill 目录的 config.json（或环境变量 FAILOVER_IMAGEGEN_TOOL_CONFIG 指定路径），
该文件包含 API Key，禁止上传或提交到仓库。
"""
import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import failovercfg


def _load_or_exit(path=None):
    try:
        cfg = failovercfg.load_config(path)
    except RuntimeError as e:
        raise SystemExit("错误：%s" % e)
    return cfg or failovercfg.default_config()


def _save_and_report(cfg, path=None):
    failovercfg.save_config(cfg, path)
    print("已保存到 %s（包含 API Key，勿提交/分享）。" % failovercfg.config_path())


def cmd_add(args):
    cfg = _load_or_exit()
    if failovercfg.find_source(cfg, args.name):
        raise SystemExit("错误：已存在同名源 %r（如需修改请先 remove 再 add）。" % args.name)
    src = failovercfg.normalize_source(
        args.name, args.base_url, args.api_key, args.model,
        enabled=not args.disabled, timeout=args.timeout,
    )
    if not src["base_url"].endswith("/v1"):
        print("提示：base_url 通常以 /v1 结尾（以服务商文档为准），当前主机：%s" % failovercfg.endpoint_host(src["base_url"]))
    cfg["sources"].append(src)
    _save_and_report(cfg)
    print("已添加源 %r，优先级第 %d。运行 failover_imagegen_tool.py --list-sources 检查。" % (src["name"], len(cfg["sources"])))


def cmd_list(_args):
    import failover_imagegen_tool
    return failover_imagegen_tool.main(["--list-sources"])


def _mutate(args, fn, verb):
    cfg = _load_or_exit()
    src = failovercfg.find_source(cfg, args.name)
    if not src:
        raise SystemExit("错误：不存在源 %r。" % args.name)
    fn(cfg, src)
    _save_and_report(cfg)
    print("已%s %r。" % (verb, args.name))


def cmd_remove(args):
    def fn(cfg, src):
        cfg["sources"].remove(src)
    _mutate(args, fn, "删除")


def cmd_enable(args):
    def fn(cfg, src):
        src["enabled"] = True
    _mutate(args, fn, "启用")


def cmd_disable(args):
    def fn(cfg, src):
        src["enabled"] = False
    _mutate(args, fn, "禁用")


def _move(args, delta, verb):
    def fn(cfg, src):
        items = cfg["sources"]
        i = items.index(src)
        j = i + delta
        if j < 0 or j >= len(items):
            raise SystemExit("错误：已经在边缘，无法%s。" % verb)
        items[i], items[j] = items[j], items[i]
    _mutate(args, fn, verb)


# ---------------------------------------------------------------- GUI

def cmd_gui(_args):
    try:
        import tkinter as tk
        from tkinter import messagebox, ttk
    except ImportError:
        raise SystemExit(
            "当前 Python 没有 tkinter，无法启动图形界面。\n"
            "请改用命令行：python setup.py add --name <名> --base-url <地址> --api-key <密钥> --model <模型>"
        )

    # ---- 现代卡片式界面：深色头部 + 表格化源列表 + 白卡片表单 ----
    C = {
        "bg": "#eef1f6", "card": "#ffffff", "line": "#e3e7ee",
        "text": "#1f2430", "muted": "#7a8299",
        "accent": "#4f6ef7", "accent_dark": "#3d5af1",
        "ok": "#1f9d63", "off": "#9aa1b3", "danger": "#e5484d",
        "head": "#232a3d", "foot": "#e4e8f0",
    }
    FONT = ("Microsoft YaHei UI", 10)
    FONT_SM = ("Microsoft YaHei UI", 9)
    FONT_TITLE = ("Microsoft YaHei UI", 15, "bold")
    FONT_BOLD = ("Microsoft YaHei UI", 10, "bold")

    # 常见 gpt 系生图模型（下拉仅是快捷选择，仍可手动输入任意模型 ID，以服务商文档为准）
    MODEL_PRESETS = [
        "gpt-image-2.5-flare", "gpt-image-2.5-sunburst", "gpt-image-2",
        "gpt-image-1", "gpt-image-1-mini", "dall-e-3", "dall-e-2",
    ]

    class SetupApp(tk.Tk):
        def __init__(self):
            super().__init__()
            self.title("中转站生图 · 源配置 — failover-imagegen-tool")
            self.geometry("980x640")
            self.minsize(660, 560)
            # 打开时居中显示，避免出现在角落让用户以为什么都没有
            self.update_idletasks()
            _sw, _sh = self.winfo_screenwidth(), self.winfo_screenheight()
            self.geometry("+%d+%d" % (max(0, (_sw - 980) // 2), max(0, (_sh - 640) // 2 - 20)))
            self.configure(bg=C["bg"])
            self.editing_index = None
            self._layout = None
            self._build_style()
            self._build_header()
            self.body = tk.Frame(self, bg=C["bg"])
            self.body.pack(fill="both", expand=True, padx=16, pady=(12, 8))
            self._build_table(self.body)
            self._build_form(self.body)
            self._apply_layout(940)  # 卡片打包方式随窗口宽度自适应
            self.bind("<Configure>", self._on_configure)
            self._build_footer()
            self.protocol("WM_DELETE_WINDOW", self.destroy)
            self._status_ready = False
            self.refresh()

        # ---- 样式 ----
        def _build_style(self):
            st = ttk.Style(self)
            st.theme_use("clam")
            st.configure(".", background=C["bg"], foreground=C["text"], font=FONT)
            st.configure("Treeview", background=C["card"], fieldbackground=C["card"],
                         foreground=C["text"], rowheight=34, borderwidth=0, font=FONT)
            st.map("Treeview", background=[("selected", C["accent"])],
                   foreground=[("selected", "#ffffff")])
            st.configure("Treeview.Heading", background="#f2f4f9", foreground=C["muted"],
                         font=FONT_SM, relief="flat", padding=(8, 6))
            st.map("Treeview.Heading", background=[("active", "#e8ebf3")])
            st.configure("TButton", background="#ffffff", foreground=C["text"],
                         borderwidth=1, focusthickness=1, focuscolor=C["line"],
                         padding=(12, 7), font=FONT)
            st.map("TButton", background=[("pressed", "#dfe4ef"), ("active", "#f0f3fa")])
            st.configure("Primary.TButton", background=C["accent"], foreground="#ffffff",
                         borderwidth=0, padding=(16, 8), font=FONT_BOLD)
            st.map("Primary.TButton",
                   background=[("pressed", C["accent_dark"]), ("active", C["accent_dark"])],
                   foreground=[("disabled", "#eef1f6")])
            st.configure("Danger.TButton", foreground=C["danger"], padding=(10, 7))
            st.map("Danger.TButton", background=[("active", "#fdecec")])
            st.configure("TEntry", fieldbackground="#ffffff", padding=6)
            st.configure("TCombobox", fieldbackground="#ffffff", background="#ffffff",
                         foreground=C["text"], padding=6, arrowcolor=C["muted"],
                         borderwidth=1)
            st.map("TCombobox", fieldbackground=[("focus", "#ffffff")],
                   bordercolor=[("focus", C["accent"])])
            self.option_add("*TCombobox*Listbox.font", FONT)
            self.option_add("*TCombobox*Listbox.background", "#ffffff")
            self.option_add("*TCombobox*Listbox.selectBackground", C["accent"])
            self.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
            st.configure("TCheckbutton", background=C["card"], foreground=C["text"],
                         focuscolor=C["card"], font=FONT)
            st.map("TCheckbutton", background=[("active", C["card"])])

        # ---- 顶部横幅 ----
        def _build_header(self):
            head = tk.Frame(self, bg=C["head"], height=66)
            head.pack(fill="x")
            head.pack_propagate(False)
            tk.Label(head, text="⛰", bg=C["head"], fg="#8fa3ff",
                     font=("Microsoft YaHei UI", 18)).pack(side="left", padx=(18, 8))
            box = tk.Frame(head, bg=C["head"])
            box.pack(side="left", fill="y", pady=11)
            tk.Label(box, text="中转站生图 · 源配置", bg=C["head"], fg="#ffffff",
                     font=FONT_TITLE).pack(anchor="w")
            tk.Label(box, text="前面的源故障（连接 / 429 / 5xx）自动切换下一个",
                     bg=C["head"], fg="#aab4d4", font=FONT_SM).pack(anchor="w")
            self.path_lbl = tk.Label(head, text="", bg=C["head"], fg="#8b94b5", font=FONT_SM)
            self.path_lbl.pack(side="right", padx=18)

        # ---- 左：源表格卡片 ----
        def _build_table(self, parent):
            card = tk.Frame(parent, bg=C["card"], highlightthickness=1,
                            highlightbackground=C["line"])
            self.table_card = card  # 打包方式由 _apply_layout 按窗口宽度决定
            tk.Label(card, text="中转源（顺序 = 切换优先级）", bg=C["card"],
                     fg=C["muted"], font=FONT_SM).pack(anchor="w", padx=14, pady=(12, 4))
            wrap = tk.Frame(card, bg=C["card"])
            wrap.pack(fill="both", expand=True, padx=10)
            cols = ("idx", "state", "name", "model")
            self.tree = ttk.Treeview(wrap, columns=cols, show="headings", selectmode="browse")
            # 不放 Base URL 列：控制表格最小宽度，保证并排时右侧表单不被挤压
            # （完整地址在选中行后由右侧表单展示）
            for cid, text, w, anchor in (("idx", "#", 36, "center"),
                                         ("state", "状态", 64, "center"),
                                         ("name", "名称", 100, "w"),
                                         ("model", "模型（级联顺序）", 220, "w")):
                self.tree.heading(cid, text=text)
                self.tree.column(cid, width=w, anchor=anchor, stretch=(cid == "model"))
            self.tree.tag_configure("on", foreground=C["ok"])
            self.tree.tag_configure("off", foreground=C["off"])
            vsb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
            self.tree.configure(yscrollcommand=vsb.set)
            self.tree.grid(row=0, column=0, sticky="nsew")
            vsb.grid(row=0, column=1, sticky="ns")
            wrap.rowconfigure(0, weight=1)
            wrap.columnconfigure(0, weight=1)
            self.tree.bind("<<TreeviewSelect>>", self._on_select)
            bar = tk.Frame(card, bg=C["card"])
            bar.pack(fill="x", padx=12, pady=10)
            self.toolbar_add = ttk.Button(bar, text="＋ 新增源", style="Primary.TButton", width=0,
                                          command=self._to_add_mode)  # 常驻入口：任何时候都能回到新增
            self.toolbar_add.pack(side="left")
            ttk.Button(bar, text="↑ 提升优先", width=0,
                       command=lambda: self._move(-1)).pack(side="left", padx=(8, 0))
            ttk.Button(bar, text="↓ 降低优先", width=0,
                       command=lambda: self._move(1)).pack(side="left", padx=(8, 0))
            ttk.Button(bar, text="删除", style="Danger.TButton", width=0,
                       command=self._remove).pack(side="left", padx=(8, 0))

        # ---- 右：表单卡片 ----
        def _build_form(self, parent):
            card = tk.Frame(parent, bg=C["card"], highlightthickness=1,
                            highlightbackground=C["line"])
            self.form_card = card  # 打包方式由 _apply_layout 按窗口宽度决定
            head = tk.Frame(card, bg=C["card"])
            head.pack(fill="x", padx=16, pady=(12, 6))
            tk.Label(head, text="源信息", bg=C["card"], fg=C["muted"],
                     font=FONT_SM).pack(side="left")
            self.mode_chip = tk.Label(head, text="新增模式", bg="#e8ecff", fg=C["accent"],
                                      font=FONT_SM, padx=10, pady=2)
            self.mode_chip.pack(side="left", padx=(10, 0))
            self.vars = {
                "name": tk.StringVar(), "base_url": tk.StringVar(),
                "api_key": tk.StringVar(), "model": tk.StringVar(),
                "timeout": tk.StringVar(),
            }
            fields = (("名称（自定义，如 example-source）", "name"),
                      ("Base URL（通常以 /v1 结尾）", "base_url"),
                      ("API Key（只保存在本机）", "api_key"),
                      ("生图模型（可逗号分隔多个，前面失败自动试后面的）", "model"),
                      ("超时秒数（可空，默认 300）", "timeout"))
            form = tk.Frame(card, bg=C["card"])
            form.pack(fill="x", padx=16)
            form.columnconfigure(0, weight=1)
            # 标签在输入框上方（现代表单布局）：右卡最小宽度大幅缩小，
            # 与左侧表格并排时输入框不会再被压缩或挤出可视区
            for i, (label, key) in enumerate(fields):
                r = i * 2
                tk.Label(form, text=label, bg=C["card"], fg=C["muted"],
                         font=FONT_SM).grid(row=r, column=0, sticky="w", pady=(10, 2))
                row = tk.Frame(form, bg=C["card"])
                row.grid(row=r + 1, column=0, sticky="we", pady=(0, 2))
                row.columnconfigure(0, weight=1)
                if key == "model":  # 模型：可手输，也可下拉选常见 gpt 系生图模型
                    ent = ttk.Combobox(row, textvariable=self.vars[key], font=FONT,
                                       values=MODEL_PRESETS)
                    self.model_box = ent
                else:
                    ent = ttk.Entry(row, textvariable=self.vars[key], font=FONT)
                ent.grid(row=0, column=0, sticky="we")
                if key == "api_key":
                    ent.config(show="•")
                    self.key_entry = ent
                    self.key_show = tk.Button(
                        row, text="显示", relief="flat", bg=C["card"], fg=C["accent"],
                        activebackground=C["card"], activeforeground=C["accent_dark"],
                        font=FONT_SM, cursor="hand2", command=self._toggle_key)
                    self.key_show.grid(row=0, column=1, padx=(6, 0))
            self.enabled = tk.BooleanVar(value=True)
            sw_row = tk.Frame(card, bg=C["card"])
            sw_row.pack(anchor="w", padx=16, pady=(12, 0))
            self.switch = tk.Label(sw_row, text="", font=FONT_SM, padx=12, pady=4,
                                   cursor="hand2")
            self.switch.bind("<Button-1>", lambda e: self._toggle_enabled())
            self.switch.pack(side="left")
            self._render_switch()
            btns = tk.Frame(card, bg=C["card"])
            btns.pack(fill="x", padx=16, pady=16)
            self.btn_add = ttk.Button(btns, text="＋  新增源", style="Primary.TButton",
                                      command=self._add)
            self.btn_save = ttk.Button(btns, text="保存修改", style="Primary.TButton",
                                       command=self._update)
            self.btn_cancel = ttk.Button(btns, text="放弃修改", command=self._to_add_mode)
            self._set_mode(False)

        # ---- 底部状态栏 ----
        def _build_footer(self):
            bar = tk.Frame(self, bg=C["foot"])
            bar.pack(fill="x", side="bottom")
            self.dot = tk.Label(bar, text="●", bg=C["foot"], fg=C["ok"], font=FONT_SM)
            self.dot.pack(side="left", padx=(14, 5), pady=7)
            self.status = tk.StringVar(value="就绪")
            tk.Label(bar, textvariable=self.status, bg=C["foot"], fg=C["text"],
                     font=FONT_SM).pack(side="left")

        def _render_switch(self):
            if self.enabled.get():
                self.switch.config(text="● 已启用（点击禁用）", bg="#e5f5ee", fg=C["ok"])
            else:
                self.switch.config(text="○ 已禁用（点击启用）", bg="#f1f2f6", fg=C["muted"])

        def _toggle_enabled(self):
            self.enabled.set(not self.enabled.get())
            self._render_switch()

        def _set_mode(self, editing):
            """新增/编辑模式分离：各自只出现自己的操作按钮。"""
            self.btn_add.pack_forget()
            self.btn_save.pack_forget()
            self.btn_cancel.pack_forget()
            if editing:
                self.mode_chip.config(text="编辑模式", bg="#fff3e0", fg="#b97a2a")
                self.btn_save.pack(side="left")
                self.btn_cancel.pack(side="left", padx=(10, 0))
            else:
                self.mode_chip.config(text="新增模式", bg="#e8ecff", fg=C["accent"])
                self.btn_add.pack(side="left")

        def _to_add_mode(self):
            if self.tree.selection():
                self.tree.selection_remove(self.tree.selection())
            self.editing_index = None
            self._clear_form()
            self._set_mode(False)

        def _on_configure(self, event):
            if event.widget is self:
                self._apply_layout(event.width)

        def _apply_layout(self, width):
            """只有两张卡片均能容纳其请求宽度时才并排显示。"""
            # body 左右各有 16px 外边距，表单左侧另有 14px 间距。
            wide_min = max(860, self.table_card.winfo_reqwidth()
                           + self.form_card.winfo_reqwidth() + 46)
            mode = "wide" if width >= wide_min else "tall"
            if mode == self._layout:
                return
            self._layout = mode
            self.table_card.pack_forget()
            self.form_card.pack_forget()
            if mode == "wide":
                self.table_card.pack(side="left", fill="both", expand=True)
                self.form_card.pack(side="left", fill="both", expand=True, padx=(14, 0))
            else:
                self.table_card.pack(side="top", fill="both", expand=True)
                self.form_card.pack(side="top", fill="x", pady=(14, 0))

        def _toggle_key(self):
            if self.key_entry.cget("show"):
                self.key_entry.config(show="")
                self.key_show.config(text="隐藏")
            else:
                self.key_entry.config(show="•")
                self.key_show.config(text="显示")

        def _set_status(self, text, kind="ok"):
            self.status.set(text)
            self.dot.config(fg=C["ok"] if kind == "ok" else C["danger"])

        # ---- 数据操作（与 CLI 同一套 failovercfg 逻辑） ----
        def _cfg(self):
            try:
                return _load_or_exit()
            except SystemExit as e:
                messagebox.showerror("配置错误", str(e))
                return failovercfg.default_config()

        def refresh(self, keep=None):
            cfg = self._cfg()
            self.tree.delete(*self.tree.get_children())
            for i, s in enumerate(cfg["sources"]):
                on = s.get("enabled", True)
                self.tree.insert("", "end", iid=str(i), tags=("on" if on else "off",),
                                 values=(i + 1, "● 启用" if on else "○ 禁用", s["name"],
                                         failovercfg.models_display(s)))
            self.path_lbl.config(text=failovercfg.config_path())
            if keep is not None and self.tree.exists(str(keep)):
                self.tree.selection_set(str(keep))
                self.tree.see(str(keep))
                self._on_select()
            elif not self.tree.selection():
                if self.editing_index is not None:
                    self._to_add_mode()
            if not cfg["sources"]:
                self._set_status("暂无中转源：在右侧填写后点「＋ 新增源」")
            elif not self._status_ready:
                self._set_status("共 %d 个源：前面的源故障（连接 / 429 / 5xx）自动切换下一个"
                                 % len(cfg["sources"]))
                self._status_ready = True

        def _clear_form(self):
            for v in self.vars.values():
                v.set("")
            self.enabled.set(True)
            self._render_switch()
            self.key_entry.config(show="•")
            self.key_show.config(text="显示")

        def _selected_index(self):
            sel = self.tree.selection()
            return int(sel[0]) if sel else None

        def _on_select(self, *_):
            idx = self._selected_index()
            if idx is None:
                return
            cfg = self._cfg()
            if idx >= len(cfg["sources"]):
                return
            self.editing_index = idx
            s = cfg["sources"][idx]
            self.vars["name"].set(s["name"])
            self.vars["base_url"].set(s["base_url"])
            self.vars["api_key"].set(s["api_key"])
            self.vars["model"].set(", ".join(failovercfg.source_models(s)))
            self.vars["timeout"].set(str(s.get("timeout", "")))
            self.enabled.set(bool(s.get("enabled", True)))
            self._render_switch()
            self._set_mode(True)
            self._set_status("正在编辑「%s」：点「保存修改」保存；要加新源随时点左下角「＋ 新增源」"
                             % s["name"])

        def _form_source(self):
            timeout = self.vars["timeout"].get().strip()
            # 模型支持逗号分隔多个（站内级联）；也可从下拉选单个
            models = [m.strip() for m in re.split(r"[,，]", self.vars["model"].get())
                      if m.strip()]
            return failovercfg.normalize_source(
                self.vars["name"].get(), self.vars["base_url"].get(),
                self.vars["api_key"].get(), models,
                enabled=self.enabled.get(), timeout=int(timeout) if timeout else None,
            )

        def _add(self):
            try:
                src = self._form_source()
            except (RuntimeError, ValueError) as e:
                messagebox.showerror("无法添加", str(e))
                self._set_status("添加失败：%s" % e, "bad")
                return
            cfg = self._cfg()
            if failovercfg.find_source(cfg, src["name"]):
                messagebox.showerror("无法添加", "已存在同名源 %r" % src["name"])
                return
            cfg["sources"].append(src)
            failovercfg.save_config(cfg)
            self._set_status("已添加「%s」并保存（优先级第 %d）。继续填写可加下一个源"
                             % (src["name"], len(cfg["sources"])))
            self.refresh()
            self._to_add_mode()

        def _update(self):
            idx = self.editing_index
            if idx is None:
                messagebox.showinfo("提示", "当前是新增模式：点击左侧某个源即可进入编辑")
                return
            try:
                src = self._form_source()
            except (RuntimeError, ValueError) as e:
                messagebox.showerror("无法保存", str(e))
                return
            cfg = self._cfg()
            for i, s in enumerate(cfg["sources"]):
                if i != idx and s["name"] == src["name"]:
                    messagebox.showerror("无法保存", "已存在同名源「%s」" % src["name"])
                    return
            cfg["sources"][idx] = src
            failovercfg.save_config(cfg)
            self._set_status("已更新「%s」并保存" % src["name"])
            self.refresh()
            self._to_add_mode()

        def _remove(self):
            idx = self._selected_index()
            if idx is None:
                return
            cfg = self._cfg()
            name = cfg["sources"][idx]["name"]
            if messagebox.askyesno("确认删除", "删除源「%s」？" % name):
                del cfg["sources"][idx]
                failovercfg.save_config(cfg)
                self._set_status("已删除「%s」" % name)
                self.refresh()
                self._to_add_mode()

        def _move(self, delta):
            idx = self._selected_index()
            if idx is None:
                return
            cfg = self._cfg()
            j = idx + delta
            if 0 <= j < len(cfg["sources"]):
                cfg["sources"][idx], cfg["sources"][j] = cfg["sources"][j], cfg["sources"][idx]
                failovercfg.save_config(cfg)
                self._set_status("已调整顺序，当前第 %d 位：%s" % (j + 1, cfg["sources"][j]["name"]))
                self.refresh(keep=j)

    app = SetupApp()
    if os.environ.get("FAILOVER_IMAGEGEN_TOOL_GUI_SMOKE"):
        # 冒烟模式：真实创建窗口、完成一次布局刷新后退出（供 tests/run_tests.py 验证）
        app.update()
        app.update_idletasks()
        app.destroy()
        print("gui smoke ok（窗口创建/刷新/销毁通过）")
        return 0
    if os.environ.get("FAILOVER_IMAGEGEN_TOOL_GUI_SELFTEST"):
        # 自测模式：真实构建界面后，程序化断言关键 UI 行为（供 tests/run_tests.py 验证）
        from tkinter import ttk as _ttk
        app.update()
        # 1) 模型下拉：可编辑 Combobox + 预置 gpt 系模型 + 手输
        assert isinstance(app.model_box, _ttk.Combobox), "模型字段应为 Combobox"
        assert tuple(app.model_box["values"]) == tuple(MODEL_PRESETS), \
            "预置模型列表不符: %s" % (app.model_box["values"],)
        app.vars["name"].set("selftest")
        app.vars["base_url"].set("https://selftest.example.com/v1")
        app.vars["api_key"].set("sk-selftest")
        app.vars["timeout"].set("")
        app.vars["model"].set("my-custom-model-x")  # 手动输入任意模型
        assert app._form_source()["model"] == "my-custom-model-x", "手动输入模型未生效"
        app.vars["model"].set(MODEL_PRESETS[0])     # 下拉选择预置模型
        assert app._form_source()["model"] == MODEL_PRESETS[0], "预置模型未生效"
        app.vars["model"].set("model-a, model-b")   # 逗号分隔多模型（站内级联）
        assert app._form_source().get("models") == ["model-a", "model-b"], "多模型解析失败"
        # 2) 新增/编辑模式分离 + 常驻新增入口：初始只有「新增源」；编辑时新增入口仍在
        assert app.btn_add.winfo_ismapped() and not app.btn_save.winfo_ismapped(), \
            "初始应为新增模式（只显示新增按钮）"
        assert app.toolbar_add.winfo_ismapped(), "工具栏应有常驻「＋ 新增源」入口"
        app.tree.selection_set("0")
        app._on_select()  # 点击行 -> 进入编辑模式
        app.update()
        assert app.btn_save.winfo_ismapped() and not app.btn_add.winfo_ismapped(), \
            "选中行后应为编辑模式（只显示保存/放弃）"
        assert app.toolbar_add.winfo_ismapped(), "编辑模式下仍必须有新增入口（不得死锁在编辑）"
        app._to_add_mode()
        app.update()
        assert app.btn_add.winfo_ismapped() and not app.btn_save.winfo_ismapped(), \
            "退出编辑后应回到新增模式"
        # 3) 启用开关：非复选框样式，文案随状态切换且不含 ×
        assert "×" not in app.switch.cget("text") and "启用" in app.switch.cget("text"), \
            "启用开关文案异常: %s" % app.switch.cget("text")
        app.enabled.set(False)
        app._render_switch()
        assert "○" in app.switch.cget("text"), "禁用态文案异常"
        app.enabled.set(True)
        app._render_switch()
        # 4) 布局自适应：窄窗口上下堆叠，宽窗口左右分栏
        app.geometry("720x580")
        app.update()
        app.update_idletasks()
        assert app.form_card.pack_info()["side"] == "top", "窄窗口应为上下布局"
        app.geometry("940x600")
        app.update()
        app.update_idletasks()
        required = app.table_card.winfo_reqwidth() + app.form_card.winfo_reqwidth() + 46
        if 940 < required:
            assert app.form_card.pack_info()["side"] == "top", \
                "两卡片需要 %dpx，940px 窗口应切换为上下布局" % required
        else:
            assert app.form_card.pack_info()["side"] == "left", "空间足够时应为左右布局"
        assert app.key_entry.winfo_ismapped(), "940px 窗口下密钥输入框不可见"
        app.geometry("1200x600")
        app.update()
        app.update_idletasks()
        assert app.form_card.pack_info()["side"] == "left", "宽窗口应为左右布局"
        # 5) 宽模式并排时，右侧表单不得被压缩到不可用（历史bug：右卡被挤没）
        fw = app.form_card.winfo_width()
        ew = app.key_entry.winfo_width()
        assert fw >= 280, "宽模式下表单卡被压缩（宽度 %dpx）" % fw
        assert ew >= 150, "宽模式下输入框被压缩（宽度 %dpx）" % ew
        app.destroy()
        print("gui selftest ok（模型下拉+手输、新增/编辑分离、启用开关、布局自适应；"
              "1200px 宽窗口下表单卡 %dpx、输入框 %dpx）" % (fw, ew))
        return 0
    app.mainloop()
    return 0


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    parser = argparse.ArgumentParser(prog="setup.py", description="failover-imagegen-tool 中转源配置管理")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("gui", help="图形配置界面")
    p.set_defaults(func=cmd_gui)

    p = sub.add_parser("add", help="添加中转源")
    p.add_argument("--name", required=True, help="自定义名称，如 example-source")
    p.add_argument("--base-url", required=True, help="OpenAI 兼容根地址，通常以 /v1 结尾")
    p.add_argument("--api-key", required=True, help="访问密钥（只保存在本机 config.json）")
    p.add_argument("--model", required=True, action="append",
                   help="生图模型 ID，可多次提供组成站内级联（如 --model a --model b），按顺序尝试")
    p.add_argument("--timeout", type=int, help="该源请求超时秒数（默认 300）")
    p.add_argument("--disabled", action="store_true", help="添加但暂不启用")
    p.set_defaults(func=cmd_add)

    for name, help_text in (("list", "列出中转源"),):
        p = sub.add_parser(name, help=help_text)
        p.set_defaults(func=cmd_list)

    for name, verb in (("remove", "删除"), ("enable", "启用"), ("disable", "禁用"),
                       ("up", "上移"), ("down", "下移")):
        p = sub.add_parser(name, help="按名称%s源" % verb)
        p.add_argument("--name", required=True)
        p.set_defaults(func=globals()["cmd_" + name])

    p = sub.add_parser("path", help="打印配置文件路径")
    p.set_defaults(func=lambda a: (print(failovercfg.config_path()), 0)[1])

    args = parser.parse_args(argv)
    if getattr(args, "func", None):
        return args.func(args)
    parser.print_help()
    return 0


def cmd_up(args):
    _move(args, -1, "上移")


def cmd_down(args):
    _move(args, 1, "下移")


if __name__ == "__main__":
    sys.exit(main())
