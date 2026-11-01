#!/usr/bin/env python3
"""IMG Switch（img-switch）— failover-imagegen-app · 本机控制台（Windows 优先）

单窗口两页面（ccswitch 风格）：
  Agent 接入 只显示本机已安装的 Agent（.agents/.zcode/.codex/.claude/.cursor/
           .trae-cn/.codebuddy/.mateclaw），每个独立开关，可只让部分 Agent 走
           中转站生图（app-settings.json 记录排除名单）。
  中转源   故障转移全局开关（开=失败自动切换下一源/模型；关=仅用★首选源）；
           配置管理（内嵌页面，不弹窗）：增删改、「参与生图」启停、★设为首选置顶、
           ▲▼调整后备顺序、站内多模型级联；模型下拉按当前 URL 实时获取；每源带「探测」。

命令行：python app.py on|off|status|gui（默认 gui；on/off/status 遵守按需接入设置）
冒烟（测试）：FAILOVER_IMAGEGEN_APP_SMOKE=1；界面自测：FAILOVER_IMAGEGEN_APP_SELFTEST=1
本机 HOME 隔离（测试）：FAILOVER_IMAGEGEN_APP_HOME=<目录>
"""
import datetime
import filecmp
import importlib.util
import json
import os
import queue
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

APP_NAME = "failover-imagegen-app"
SKILL_NAME = "failover-imagegen-tool"
MARKER = ".installed-by-app"
# (宿主目录, 说明)——.agents 为跨工具标准目录，始终安装；其余仅当宿主目录存在时安装
# 宿主目录即「该 Agent 的用户级 skills 目录」的父目录（~/.<host>/skills/）
AGENT_HOSTS = [
    (".agents", "跨工具标准 / ZCode"),
    (".zcode", "ZCode"),
    (".codex", "Codex"),
    (".claude", "Claude Code"),
    (".cursor", "Cursor"),
    (".trae-cn", "Trae"),
    (".codebuddy", "CodeBuddy"),
    (".mateclaw", "MateClaw"),
]
# 生图模型过滤（与引擎 IMAGE_HINT 保持一致；「⟳ 获取」拉取 /v1/models 后用它排序）
IMAGE_HINT = re.compile(
    r"image|dall|flux|midjourney|\bmj\b|stable|sdxl|sd3|seedream|ideogram|recraft"
    r"|kolors|hidream|pixart|imagen|wanx|imagine|banana|leo", re.IGNORECASE)


def probe_relay(base, headers):
    """Check the model-list endpoint, retrying a transient connection failure once."""
    req = urllib.request.Request(base.rstrip('/') + '/models', headers=headers)
    t0 = time.monotonic()
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=12):
                pass
            return '✓ %dms' % int((time.monotonic() - t0) * 1000), '模型接口响应正常', True
        except urllib.error.HTTPError as error:
            if error.code == 401:
                return '✗ 鉴权失败', '模型接口返回 HTTP 401，请检查密钥', False
            if error.code == 403:
                return '✗ 请求被拒绝', '模型接口返回 HTTP 403，请检查服务权限或安全策略', False
            return '△ HTTP %d' % error.code, '模型接口返回 HTTP %d' % error.code, False
        except (urllib.error.URLError, OSError) as error:
            reason = getattr(error, 'reason', error)
            if isinstance(reason, ssl.SSLCertVerificationError):
                return '✗ 证书错误', 'HTTPS 证书验证失败', False
            if attempt == 0:
                time.sleep(0.4)
                continue
            if isinstance(reason, (socket.timeout, TimeoutError)):
                return '✗ 连接超时', '模型接口连接超时（已重试）', False
            if isinstance(reason, socket.gaierror):
                return '✗ 域名解析失败', '无法解析中转站域名（已重试）', False
            return '✗ 连接失败', '无法连接模型接口（已重试）', False
    return '✗ 连接失败', '无法连接模型接口', False


# ---------------------------------------------------------------- 路径与状态

def home():
    return os.environ.get("FAILOVER_IMAGEGEN_APP_HOME") or os.path.expanduser("~")


def appdata_dir():
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.join(
            home(), "AppData", "Roaming")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(home(), ".config")
    return os.path.join(base, "failover-imagegen-tool")


def appdata_config():
    return os.path.join(appdata_dir(), "config.json")


def engine_dir():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "engine")


def skill_dst(host_dir):
    return os.path.join(home(), host_dir, "skills", SKILL_NAME)


def agent_targets():
    """返回 [(host_dir, 说明, skills根, 是否会安装)]。"""
    out = []
    for host, label in AGENT_HOSTS:
        host_path = os.path.join(home(), host)
        active = host == ".agents" or os.path.isdir(host_path)
        out.append((host, label, os.path.join(host_path, "skills"), active))
    return out


def _is_our_skill(dst):
    try:
        if not os.path.isfile(os.path.join(dst, MARKER)):
            return False
        with open(os.path.join(dst, "SKILL.md"), "r", encoding="utf-8") as f:
            return ("name: " + SKILL_NAME) in f.read()
    except OSError:
        return False


def host_hooked(host):
    """该 Agent 当前是否已接入（装有本 skill）。"""
    dst = skill_dst(host)
    return os.path.isdir(dst) and _is_our_skill(dst)


def is_on():
    # 任一 Agent 目录装有本 skill 即视为开启（支持部分接入）
    return any(host_hooked(h) for h, _l, _r, _a in agent_targets())


# ---------------------------------------------------------------- 按需接入设置

def app_settings_path():
    return os.path.join(appdata_dir(), "app-settings.json")


def load_app_settings():
    try:
        with open(app_settings_path(), "r", encoding="utf-8") as f:
            doc = json.load(f)
        if isinstance(doc, dict):
            return doc
    except (OSError, ValueError):
        pass
    return {}


def save_app_settings(doc):
    os.makedirs(appdata_dir(), exist_ok=True)
    with open(app_settings_path(), "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)


def host_disabled(host):
    """该 Agent 是否被用户明确排除（默认全部接入）。"""
    return host in (load_app_settings().get("disabled_hosts") or [])


def set_host_enabled(host, enabled):
    doc = load_app_settings()
    dis = [h for h in (doc.get("disabled_hosts") or []) if h != host]
    if not enabled:
        dis.append(host)
    doc["disabled_hosts"] = dis
    save_app_settings(doc)


# ---------------------------------------------------------------- 安装 / 卸载

def _migrate_local_config():
    if os.path.isfile(appdata_config()):
        return None
    for host, _label, _root, active in agent_targets():
        if not active:
            continue
        local = os.path.join(skill_dst(host), "config.json")
        if _is_our_skill(skill_dst(host)) and os.path.isfile(local):
            os.makedirs(appdata_dir(), exist_ok=True)
            try:
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
                fd = os.open(appdata_config(), flags, 0o600)
            except FileExistsError:
                return None  # Never overwrite a config created concurrently.
            try:
                with os.fdopen(fd, "wb") as target, open(local, "rb") as source:
                    shutil.copyfileobj(source, target)
                    target.flush()
                    os.fsync(target.fileno())
            except Exception:
                os.unlink(appdata_config())
                raise
            return appdata_config()
    return None


def _backup_local_configs(dst):
    """Preserve legacy local secrets before replacing or removing a managed skill."""
    for filename in ("config.json", "config.json.tmp"):
        source = os.path.join(dst, filename)
        if not os.path.isfile(source):
            continue
        directory = os.path.join(appdata_dir(), "migration-backups")
        os.makedirs(directory, mode=0o700, exist_ok=True)
        fd, backup = tempfile.mkstemp(prefix=filename + "-", suffix=".backup", dir=directory)
        try:
            with os.fdopen(fd, "wb") as target, open(source, "rb") as original:
                shutil.copyfileobj(original, target)
                target.flush()
                os.fsync(target.fileno())
        except Exception:
            os.unlink(backup)
            raise


def _prepare_packaged_skill(path):
    """Use the packaged console engine in installed skills, when present."""
    binary = "img-switch-engine.exe" if os.name == "nt" else "img-switch-engine"
    if not os.path.isfile(os.path.join(path, "bin", binary)):
        return
    skill_path = os.path.join(path, "SKILL.md")
    with open(skill_path, "r", encoding="utf-8") as f:
        text = f.read()
    old = "python <skill_dir>/scripts/failover_imagegen_tool.py"
    if old not in text:
        raise RuntimeError("技能说明缺少引擎入口，无法安全部署")
    text = text.replace(old, '"<skill_dir>/bin/%s"' % binary)
    text = text.replace(
        "脚本位于本 skill 目录 `scripts/`（下文 `<skill_dir>` 即 SKILL.md 所在目录）。",
        "独立引擎位于本 skill 目录 `bin/`，无需系统 Python；下文 `<skill_dir>` 为 SKILL.md 所在目录，请替换为实际路径并保留引号。")
    if os.name == "nt":
        text = text.replace(
            "## 使用前：确认已配置中转源",
            '以下命令示例使用 Bash；PowerShell 中需用调用运算符，例如 `& "<skill_dir>/bin/img-switch-engine.exe" --list-sources`，并把多行命令合并为一行。\n\n## 使用前：确认已配置中转源')
    text = re.sub(r"```bash\npython <skill_dir>/scripts/setup\.py add.*?\n```",
                  "请使用 IMG Switch 控制台添加和管理中转源。", text, count=1, flags=re.DOTALL)
    text = text.replace("站顺序用 `setup.py up/down` 调整", "站顺序在 IMG Switch 控制台调整")
    text = text.replace("需要新增/调整源用 `setup.py` 子命令", "需要新增/调整源时使用 IMG Switch 控制台")
    with open(skill_path, "w", encoding="utf-8") as f:
        f.write(text)


def install_skill(hosts=None):
    """安装到指定宿主集合；hosts=None 表示全部「未被排除」的已检测 Agent。"""
    src = engine_dir()
    if not os.path.isfile(os.path.join(src, "SKILL.md")):
        raise RuntimeError("引擎目录不完整：%s" % src)
    _migrate_local_config()
    installed = []
    for host, _label, root, active in agent_targets():
        if not active:
            continue
        if hosts is None and host_disabled(host):
            continue  # 用户明确不接入该 Agent
        if hosts is not None and host not in hosts:
            continue
        dst = skill_dst(host)
        if os.path.lexists(dst) and (not os.path.isdir(dst) or not _is_our_skill(dst)):
            continue  # 非我们管理的目录，跳过不动
        if os.path.isdir(dst):
            _backup_local_configs(dst)
        os.makedirs(root, exist_ok=True)
        # Stage first so copy or preparation failures leave the existing skill intact.
        work = tempfile.mkdtemp(prefix=".img-switch-stage-", dir=root)
        staged = os.path.join(work, "new")
        backup = os.path.join(work, "old")
        committed = False
        try:
            shutil.copytree(src, staged, ignore=shutil.ignore_patterns(
                "__pycache__", "config.json", "config.json.tmp", "*.pyc"))
            _prepare_packaged_skill(staged)
            with open(os.path.join(staged, MARKER), "w", encoding="utf-8") as f:
                f.write("installed-by %s at %s\n" % (APP_NAME, datetime.datetime.now().isoformat()))
            had_previous = os.path.isdir(dst)
            if had_previous:
                os.replace(dst, backup)
            try:
                os.replace(staged, dst)
                committed = True
            except Exception as install_error:
                if had_previous:
                    try:
                        os.replace(backup, dst)
                    except Exception as rollback_error:
                        raise RuntimeError("安装和恢复失败，旧技能保留在 %s：%s" %
                                           (backup, rollback_error)) from install_error
                raise
        finally:
            # A failed rollback must never delete the only remaining old copy.
            if committed or not os.path.lexists(backup):
                shutil.rmtree(work, ignore_errors=True)
        installed.append(dst)
    return installed


def sync_connected_skills():
    """Upgrade only already-connected managed skills to this packaged release."""
    source = engine_dir()
    binary = "img-switch-engine.exe" if os.name == "nt" else "img-switch-engine"
    if not os.path.isfile(os.path.join(source, "bin", binary)):
        return []  # Source installations have no bundled runtime to propagate.
    failures = []
    for host, _label, _root, active in agent_targets():
        if not active or host_disabled(host) or not host_hooked(host):
            continue
        try:
            with tempfile.TemporaryDirectory(prefix="img-switch-compare-") as work:
                expected = os.path.join(work, "expected")
                shutil.copytree(source, expected, ignore=shutil.ignore_patterns(
                    "__pycache__", "config.json", "config.json.tmp", "*.pyc"))
                _prepare_packaged_skill(expected)
                destination = skill_dst(host)
                current = True
                for root, _dirs, files in os.walk(expected):
                    for name in files:
                        expected_file = os.path.join(root, name)
                        installed_file = os.path.join(destination, os.path.relpath(expected_file, expected))
                        if not os.path.isfile(installed_file) or not filecmp.cmp(
                                expected_file, installed_file, shallow=False):
                            current = False
                            break
                    if not current:
                        break
                if not current and not install_skill([host]):
                    raise RuntimeError("未能更新已接入的技能")
        except Exception as error:  # noqa: BLE001
            failures.append((host, type(error).__name__))
    return failures


def remove_skill(hosts=None):
    """移除指定宿主集合的本 skill；hosts=None 表示全部（安全兜底，off 用）。"""
    _migrate_local_config()
    removed = []
    for host, _label, _root, active in agent_targets():
        if not active:
            continue
        if hosts is not None and host not in hosts:
            continue
        dst = skill_dst(host)
        if os.path.isdir(dst) and _is_our_skill(dst):
            _backup_local_configs(dst)
            shutil.rmtree(dst)
            removed.append(dst)
    return removed


# ---------------------------------------------------------------- 引擎调用

def engine_script(name):
    return os.path.join(engine_dir(), "scripts", name)


def self_exe_cmd():
    """启动自身的命令前缀：exe 版是 [exe]，绿色版是 [python, app.py]。"""
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, os.path.abspath(__file__)]


def load_engine_module(filename, modname):
    path = engine_script(filename)
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod


def engine_env():
    env = dict(os.environ)
    env["FAILOVER_IMAGEGEN_TOOL_CONFIG"] = appdata_config()
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def run_engine_output(args, timeout=90):
    """在子进程运行引擎命令并返回 (rc, stdout+stderr)（管道+文件双通道）。"""
    fd, out_file = tempfile.mkstemp(prefix="figtool_", suffix=".txt")
    os.close(fd)
    env = engine_env()
    env["FAILOVER_IMAGEGEN_CHILD_OUT"] = out_file
    try:
        p = subprocess.run(self_exe_cmd() + ["__tool"] + args,
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env=env, timeout=timeout)
        out = (p.stdout or "") + (p.stderr or "")
        if not out.strip():
            try:
                with open(out_file, "r", encoding="utf-8", errors="replace") as f:
                    out = f.read()
            except OSError:
                out = ""
        return p.returncode, out
    finally:
        try:
            os.remove(out_file)
        except OSError:
            pass


# ---------------------------------------------------------------- CLI

def cmd_on():
    if is_on() and not os.environ.get("FAILOVER_IMAGEGEN_APP_FORCE"):
        print("已处于开启状态。如需重装：设置 FAILOVER_IMAGEGEN_APP_FORCE=1 后再执行 on。")
    installed = install_skill()
    for d in installed:
        print("  ✓ 已安装：%s" % d)
    excluded = [l for h, l, _r, a in agent_targets() if a and host_disabled(h)]
    if excluded:
        print("已按设置排除：%s（在界面「总开关」页可单独接入）" % "、".join(excluded))
    print("开启完成：已接入的 Agent 在任意项目下可对话触发生图；"
          "配置统一保存在 %s" % appdata_config())
    if not os.path.isfile(appdata_config()):
        print("（尚未配置中转源：运行 python \"%s\" gui 配置）" % os.path.abspath(__file__))
    return 0


def cmd_off():
    if not is_on():
        print("已处于关闭状态。")
        return 0
    removed = remove_skill()
    for d in removed:
        print("  ✓ 已移除：%s" % d)
    print("关闭完成：本机已恢复原状（各 Agent 用回自己套餐的生图能力）。"
          "配置保留在 %s，重新 on 即恢复。" % appdata_config())
    return 0


def cmd_status():
    state = "已开启" if is_on() else "已关闭"
    print("状态：%s（任一 Agent 接入即视为开启，支持部分接入）" % state)
    print("统一配置：%s%s" % (appdata_config(),
                              "（已存在）" if os.path.isfile(appdata_config()) else "（未创建）"))
    try:
        with open(appdata_config(), "r", encoding="utf-8") as f:
            fai = json.load(f).get("failover", True)
        print("故障转移：%s（%s）" % ("开启" if fai else "关闭",
                                    "失败自动切换下一源/模型" if fai else "仅用首位源首个模型"))
    except (OSError, ValueError):
        pass
    for host, label, _root, active in agent_targets():
        dst = skill_dst(host)
        if not active:
            print("  — %-8s 未检测到该 Agent（宿主目录 %s 不存在）" % (label, host))
        elif os.path.isdir(dst) and _is_our_skill(dst):
            print("  ✓ %-8s 已接入 %s" % (label, dst))
        elif host_disabled(host):
            print("  ✗ %-8s 已按设置排除（界面「总开关」页可接入）" % label)
        else:
            print("  ○ %-8s Agent 存在，当前未接入（off 状态）" % label)
    return 0


# ---------------------------------------------------------------- GUI

def cmd_gui():
    try:
        import tkinter as tk
        from tkinter import messagebox, ttk
    except ImportError:
        raise SystemExit("当前 Python 缺少 tkinter，请改用 CLI：python app.py on/off/status")

    # ccswitch 风格设计系统：浅灰底 + 白卡片 + 橙色主按钮 + 绿色状态色
    C = {
        "bg": "#f7f7f8", "side": "#fbfbfc", "card": "#ffffff", "line": "#e5e7eb",
        "text": "#1f2937", "muted": "#6b7280", "light": "#9ca3af",
        "primary": "#f97316", "primary_dark": "#ea680c", "primary_light": "#fff3e8",
        "ok": "#10b981", "ok_light": "#ecfdf5", "ok_line": "#a7f3d0",
        "warn": "#d97706", "warn_light": "#fffbeb",
        "danger": "#ef4444", "danger_light": "#fef2f2",
        "off": "#9ca3af", "select": "#f0f0f0", "seg": "#f3f4f6",
        "track_off": "#d1d5db",
    }
    FONT = ("Microsoft YaHei UI", 10)
    FONT_SM = ("Microsoft YaHei UI", 9)
    FONT_TITLE = ("Microsoft YaHei UI", 15, "bold")
    FONT_BOLD = ("Microsoft YaHei UI", 10, "bold")
    AVATARS = {
        ".agents": ("跨", "#64748b"), ".zcode": ("Z", "#3b82f6"),
        ".codex": ("C", "#10b981"), ".claude": ("C", "#d97706"),
        ".cursor": ("X", "#ec4899"), ".trae-cn": ("T", "#7c5cff"),
        ".codebuddy": ("B", "#1e6fff"), ".mateclaw": ("M", "#e11d48"),
    }
    SRC_COLORS = ["#f97316", "#3b82f6", "#10b981", "#8b5cf6", "#ec4899", "#14b8a6"]

    def rrect(cv, x1, y1, x2, y2, r, **kw):
        pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
               x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
        return cv.create_polygon(pts, smooth=True, **kw)

    def avatar(parent, letter, color, size=30, bg=None, font_px=12):
        """ccswitch 式圆角方形图标。"""
        cv = tk.Canvas(parent, width=size, height=size, bg=bg or C["card"],
                       highlightthickness=0)
        rrect(cv, 1, 1, size - 1, size - 1, 8, fill=color, outline="")
        cv.create_text(size / 2, size / 2, text=letter, fill="#ffffff",
                       font=("Microsoft YaHei UI", font_px, "bold"))
        return cv

    def flat_btn(parent, text, command, fg=None, bg=None, padx=10, pady=3,
                 font=None):
        """ccswitch 式扁平小文字按钮（悬停浅灰底）。"""
        bg = bg or parent.cget("bg")
        color = fg or C["muted"]
        # Aqua draws a native rounded bezel for tk.Button even with relief=flat.
        # Labels retain the card's exact background on both macOS and Windows.
        b = tk.Label(parent, text=text, bd=0, bg=bg, fg=color, takefocus=True,
                     font=font or FONT_SM, cursor="hand2", padx=padx, pady=pady)
        b.bind("<Enter>", lambda e: b.config(bg=C["seg"]))
        b.bind("<Leave>", lambda e: b.config(bg=bg))
        b.bind("<Button-1>", lambda e: command())
        b.bind("<Return>", lambda e: command())
        b.bind("<space>", lambda e: command())
        b.bind("<FocusIn>", lambda e: b.config(bg=C["seg"]))
        b.bind("<FocusOut>", lambda e: b.config(bg=bg))
        return b

    def pill(parent, text, fg, bg, padx=10, pady=3):
        return tk.Label(parent, text=text, fg=fg, bg=bg, font=FONT_SM,
                        padx=padx, pady=pady)

    def page_top(page, title, subtitle=""):
        """页面顶栏：左侧大标题(+副标题)，返回右侧操作按钮容器（ccswitch 布局）。"""
        bar = tk.Frame(page, bg=C["bg"])
        bar.pack(fill="x", pady=(2, 10))
        box = tk.Frame(bar, bg=C["bg"])
        box.pack(side="left")
        tk.Label(box, text=title, bg=C["bg"], fg=C["text"], font=FONT_TITLE,
                 anchor="w").pack(anchor="w")
        if subtitle:
            tk.Label(box, text=subtitle, bg=C["bg"], fg=C["muted"], font=FONT_SM,
                     anchor="w").pack(anchor="w")
        right = tk.Frame(bar, bg=C["bg"])
        right.pack(side="right")
        return right

    def failover_now():
        """读 config.json 顶层 failover（默认 True）。"""
        try:
            with open(appdata_config(), "r", encoding="utf-8") as f:
                return bool(json.load(f).get("failover", True))
        except (OSError, ValueError):
            return True

    class Switch(tk.Canvas):
        """iOS 风格滑块开关：窄胶囊轨道 + 带阴影的白色滑块 + 位移动画。"""

        def __init__(self, master, on_change, width=64, height=32):
            super().__init__(master, width=width, height=height, bg=C["card"],
                             highlightthickness=0, cursor="hand2")
            self.w, self.h = width, height
            self.state = False
            self.on_change = on_change
            self.knob = height - 8   # 滑块直径：四周各留 4px
            self.pad = 4
            self.pos = self.pad
            self.bind("<Button-1>", lambda e: self.toggle())
            self.draw()

        def set_state(self, on, animate=True):
            self.state = on
            target = self.w - self.knob - self.pad if on else self.pad
            if not animate:
                self.pos = target
                self.draw()
                return
            start = self.pos
            steps = 8

            def step(i):
                self.pos = start + (target - start) * i / steps
                self.draw()
                if i < steps:
                    self.after(12, lambda: step(i + 1))
            step(1)

        def toggle(self):
            self.state = not self.state
            self.on_change(self.state)

        def _round_rect(self, x1, y1, x2, y2, r, **kw):
            pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
                   x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
            return self.create_polygon(pts, smooth=True, **kw)

        def draw(self):
            self.delete("all")
            track = C["ok"] if self.state else "#e5e7eb"
            self._round_rect(0, 0, self.w, self.h, self.h / 2, fill=track, outline="")
            cy = self.h / 2
            cx = self.pos + self.knob / 2
            kr = self.knob / 2 - 1
            # 滑块投影（随轨道色加深）+ 白色滑块
            shadow = "#0f9d6c" if self.state else "#d1d5db"
            self.create_oval(cx - kr + 1, cy - kr + 2, cx + kr + 1, cy + kr + 2,
                             fill=shadow, outline="")
            self.create_oval(cx - kr, cy - kr, cx + kr, cy + kr,
                             fill="#ffffff", outline="")

    # ---- 页面基类 ----
    class Page(tk.Frame):
        def __init__(self, master, app):
            super().__init__(master, bg=C["bg"])
            self.app = app

    class ScrollBox(tk.Frame):
        """卡片流滚动容器：内容超出可视高度才显示滚动条（滚轮由主窗口统一路由）。"""

        def __init__(self, master):
            super().__init__(master, bg=C["bg"])
            self.cv = tk.Canvas(self, bg=C["bg"], highlightthickness=0)
            self.bar = ttk.Scrollbar(self, orient="vertical", command=self.cv.yview)
            self.inner = tk.Frame(self.cv, bg=C["bg"])
            win = self.cv.create_window((0, 0), window=self.inner, anchor="nw")
            self.cv.configure(yscrollcommand=self.bar.set)
            self.cv.pack(side="left", fill="both", expand=True)
            self.inner.bind("<Configure>", self._on_content)
            self.cv.bind("<Configure>", lambda e, w=win: (
                self.cv.itemconfigure(w, width=e.width), self._fit()))

        def _on_content(self, _e):
            self.cv.configure(scrollregion=self.cv.bbox("all"))
            self._fit()

        def _fit(self):
            """内容高度 ≤ 可视高度时隐藏滚动条。"""
            view_h = self.cv.winfo_height()
            if view_h <= 1:
                return
            need = self.inner.winfo_reqheight() > view_h
            if need and not self.bar.winfo_ismapped():
                self.bar.pack(side="right", fill="y")
            elif not need and self.bar.winfo_ismapped():
                self.bar.pack_forget()

    # ---- 页 1：Agent 接入（只显示本机已安装的 Agent） ----
    class DashboardPage(Page):
        def __init__(self, master, app):
            super().__init__(master, app)
            self.cover_rows = []
            self.agent_switches = []
            page_top(self, "Agent 接入",
                     "每个 Agent 独立接入/断开；本机未安装的 Agent 不显示")
            head = tk.Frame(self, bg=C["bg"])
            head.pack(fill="x", pady=(8, 6))
            tk.Label(head, text="本机检测到的 Agent", bg=C["bg"], fg=C["muted"],
                     font=FONT_SM).pack(side="left", padx=4)
            right = tk.Frame(head, bg=C["bg"])
            right.pack(side="right")
            flat_btn(right, "全部接入", app.connect_all, fg=C["primary"]).pack(side="left")
            flat_btn(right, "全部断开", app.disconnect_all,
                     fg=C["danger"]).pack(side="left", padx=(4, 4))
            self.cover_box = tk.Frame(self, bg=C["bg"])
            self.cover_box.pack(fill="both", expand=True)

        def refresh(self):
            for w in self.cover_box.winfo_children():
                w.destroy()
            self.cover_rows = []
            self.agent_switches = []
            hooked_n = 0
            for host, label, _root, active in agent_targets():
                if not active:
                    continue  # 本机未安装该 Agent：不显示
                row = tk.Frame(self.cover_box, bg=C["card"], highlightthickness=1,
                               highlightbackground=C["line"])
                row.pack(fill="x", pady=(0, 6), padx=2)
                self.cover_rows.append(row)
                inner = tk.Frame(row, bg=C["card"])
                inner.pack(fill="x", padx=14, pady=10)
                letter, color = AVATARS.get(host, ("?", "#64748b"))
                avatar(inner, letter, color).pack(side="left", padx=(0, 10))
                info = tk.Frame(inner, bg=C["card"])
                info.pack(side="left")
                tk.Label(info, text=label, bg=C["card"], fg=C["text"],
                         anchor="w", font=FONT_BOLD).pack(anchor="w")
                hooked = host_hooked(host)
                if hooked:
                    hooked_n += 1
                if hooked:
                    note, fg = "已接入：对话生图走中转站", C["ok"]
                elif host_disabled(host):
                    note, fg = "已排除：保持该 Agent 原生生图", C["muted"]
                else:
                    note, fg = "未接入：点右侧开关接入", C["muted"]
                tk.Label(info, text=note, bg=C["card"], fg=fg, anchor="w",
                         font=FONT_SM).pack(anchor="w")
                sw = Switch(inner, lambda on, h=host: self.app.toggle_host(h, on),
                            width=40, height=22)
                sw.set_state(hooked, animate=False)
                sw.pack(side="right")
                self.agent_switches.append(sw)
            self.app.side_state.config(
                text=("● %d 个 Agent 接入中" % hooked_n) if hooked_n else "○ 未接入任何 Agent",
                fg=C["ok"] if hooked_n else C["light"])

    # ---- 页 2：中转源（ccswitch 供应商卡片流 + 内嵌展开编辑，不弹窗） ----
    class SourcesPage(Page):
        def __init__(self, master, app):
            super().__init__(master, app)
            self.editing_index = None
            self._form_visible = False
            self._key_visible = False
            self._card_refs = []
            right = page_top(self, "中转源",
                             "★ 首选 = 生图第一个尝试的源（与卡片位置无关）；"
                             "「参与生图」= 是否加入级联；▲▼ 只调整后备顺序")
            self.btn_new = ttk.Button(right, text="＋ 添加中转源",
                                      style="Primary.TButton", command=self.open_add)
            self.btn_new.pack(side="right")

            # 故障转移开关条（全局：失败是否切换下一源/模型）
            fai = tk.Frame(self, bg=C["card"], highlightthickness=1,
                           highlightbackground=C["line"])
            fai.pack(fill="x", padx=2, pady=(0, 8))
            self.fai_switch = Switch(fai, lambda on: app.set_failover(on),
                                     width=44, height=24)
            self.fai_switch.pack(side="left", padx=(14, 12), pady=10)
            tk.Label(fai, text="故障转移", bg=C["card"], fg=C["text"],
                     font=FONT_BOLD).pack(side="left")
            tk.Label(fai, text="开：当前源/模型失败自动切换下一个重试 ｜ "
                               "关：只用★首选源，失败直接报错",
                     bg=C["card"], fg=C["muted"], font=FONT_SM).pack(
                side="left", padx=(12, 0))

            self.scroll = ScrollBox(self)
            self.scroll.pack(fill="both", expand=True)
            self.vars = {k: tk.StringVar() for k in
                         ("name", "base_url", "api_key", "model", "timeout")}
            self.enabled = tk.BooleanVar(value=True)
            self.refresh()

        # ---- 对外接口（自测 / 模型发现页使用） ----
        def src_count(self):
            return len(self._card_refs)

        def form_open(self):
            return self._form_visible

        def prefill_model(self, model_id):
            """模型发现页「填入」：打开新增表单并预填模型。"""
            self.open_add()
            self.vars["model"].set(model_id)

        # ---- 数据 ----
        def _cfg(self):
            try:
                return self.app.cfg_mod().load_config(appdata_config()) \
                    or {"version": 1, "sources": []}
            except RuntimeError as e:
                messagebox.showerror("配置错误", str(e), parent=self)
                return {"version": 1, "sources": []}

        def _save(self, cfg):
            self.app.cfg_mod().save_config(cfg, appdata_config())

        # ---- 渲染 ----
        def update_failover_ui(self):
            """仅同步故障转移开关状态，不重建页面（开关回调里安全调用）。"""
            self.fai_switch.set_state(failover_now(), animate=False)

        def refresh(self):
            self.update_failover_ui()
            for w in self.scroll.inner.winfo_children():
                w.destroy()
            self._card_refs = []
            cfg = self._cfg()
            sources = cfg["sources"]
            if self.editing_index is not None and self.editing_index >= len(sources):
                self.editing_index = None
                self._form_visible = False
            if self._form_visible:
                self._build_form()
            first_on = self.app.cfg_mod().prefer_index(cfg)
            for i, s in enumerate(sources):
                self._build_card(i, s, prefer=(i == first_on), total=len(sources))
            if not sources and not self._form_visible:
                empty = tk.Frame(self.scroll.inner, bg=C["card"],
                                 highlightthickness=1, highlightbackground=C["line"])
                empty.pack(fill="x", padx=2)
                tk.Label(empty, text="还没有中转源：点右上「＋ 添加中转源」新建第一条配置",
                         bg=C["card"], fg=C["light"], font=FONT, pady=20).pack()
            self.btn_new.config(state="disabled" if self._form_visible else "normal")

        def _build_card(self, i, s, prefer, total):
            m = self.app.cfg_mod()
            bg = C["ok_light"] if prefer else C["card"]
            border = C["ok_line"] if prefer else C["line"]
            card = tk.Frame(self.scroll.inner, bg=bg, highlightthickness=1,
                            highlightbackground=border)
            card.pack(fill="x", pady=(0, 8), padx=2)
            self._card_refs.append(card)
            row = tk.Frame(card, bg=bg)
            row.pack(fill="x", padx=12, pady=8)
            # 左侧排序导轨：▲▼（到边界时置灰不可点）
            rail = tk.Frame(row, bg=bg)
            rail.pack(side="left", padx=(0, 8))
            if i > 0:
                flat_btn(rail, "▲", lambda idx=i: self._move(idx, -1),
                         padx=3, pady=1, font=("Microsoft YaHei UI", 8)).pack()
            else:
                tk.Label(rail, text="▲", bg=bg, fg=C["line"],
                         font=("Microsoft YaHei UI", 8)).pack(pady=1)
            if i < total - 1:
                flat_btn(rail, "▼", lambda idx=i: self._move(idx, 1),
                         padx=3, pady=1, font=("Microsoft YaHei UI", 8)).pack()
            else:
                tk.Label(rail, text="▼", bg=bg, fg=C["line"],
                         font=("Microsoft YaHei UI", 8)).pack(pady=1)
            avatar(row, s["name"][:1].upper(), SRC_COLORS[i % len(SRC_COLORS)],
                   bg=bg).pack(side="left", padx=(0, 10))
            info = tk.Frame(row, bg=bg)
            info.pack(side="left")
            line1 = tk.Frame(info, bg=bg)
            line1.pack(anchor="w")
            tk.Label(line1, text=s["name"], bg=bg, fg=C["text"],
                     font=FONT_BOLD).pack(side="left")
            if prefer:
                tk.Label(line1, text="★ 首选", bg=bg, fg=C["ok"],
                         font=FONT_BOLD).pack(side="left", padx=(10, 0))
            else:
                flat_btn(line1, "☆ 设为首选", lambda idx=i: self._make_prefer(idx),
                         fg=C["primary"], padx=6, pady=0).pack(side="left", padx=(8, 0))
            on = s.get("enabled", True)
            en = pill(line1, "● 参与生图" if on else "○ 已禁用",
                      C["ok"] if on else C["off"],
                      C["card"] if on else C["seg"], padx=8)
            en.config(highlightthickness=1,
                      highlightbackground=C["ok_line"] if on else C["line"],
                      cursor="hand2")
            en.bind("<Button-1>", lambda e, idx=i: self._toggle_enabled_at(idx))
            en.pack(side="left", padx=(8, 0))
            tk.Label(info, text="%s  ｜  %s" % (m.endpoint_host(s["base_url"]),
                                                m.models_display(s)),
                     bg=bg, fg=C["muted"], font=FONT_SM, anchor="w").pack(anchor="w")
            tail = tk.Frame(row, bg=bg)
            tail.pack(side="right")
            probe_lbl = tk.Label(tail, text="", bg=bg, fg=C["muted"], font=FONT_SM)
            probe_lbl.pack(side="left", padx=(0, 6))
            ops = tk.Frame(tail, bg=bg)
            ops.pack(side="left")
            flat_btn(ops, "探测", lambda idx=i, lbl=probe_lbl: self._probe(idx, lbl),
                     bg=bg).pack(side="left")
            flat_btn(ops, "编辑", lambda idx=i: self._edit(idx), bg=bg).pack(side="left")
            flat_btn(ops, "删除", lambda idx=i: self._remove(idx),
                     fg=C["danger"], bg=bg).pack(side="left")

        # ---- 内嵌编辑卡（新增/编辑共用，保存后收起） ----
        def _build_form(self):
            editing = self.editing_index is not None
            card = tk.Frame(self.scroll.inner, bg=C["card"], highlightthickness=1,
                            highlightbackground=C["primary"])
            card.pack(fill="x", pady=(0, 10), padx=2)
            head = tk.Frame(card, bg=C["card"])
            head.pack(fill="x", padx=16, pady=(12, 2))
            tk.Label(head, text="编辑中转源" if editing else "新增中转源",
                     bg=C["card"], fg=C["text"],
                     font=("Microsoft YaHei UI", 11, "bold")).pack(side="left")
            pill(head, "编辑模式" if editing else "新增模式",
                 C["warn"] if editing else C["primary"],
                 C["warn_light"] if editing else C["primary_light"], padx=8).pack(
                side="left", padx=(10, 0))
            fields = (("名称（如 example-source）", "name"),
                      ("Base URL（通常以 /v1 结尾）", "base_url"),
                      ("API Key（只保存在本机）", "api_key"),
                      ("生图模型（可逗号分隔多个，依次级联）", "model"),
                      ("超时秒数（可空，默认 300）", "timeout"))
            grid = tk.Frame(card, bg=C["card"])
            grid.pack(fill="x", padx=16, pady=(6, 0))
            grid.columnconfigure(1, weight=1)
            for r, (label, key) in enumerate(fields):
                tk.Label(grid, text=label, bg=C["card"], fg=C["muted"],
                         font=FONT_SM).grid(row=r, column=0, sticky="w", pady=6)
                rowf = tk.Frame(grid, bg=C["card"])
                rowf.grid(row=r, column=1, sticky="we", pady=6, padx=(12, 0))
                rowf.columnconfigure(0, weight=1)
                if key == "model":
                    ent = ttk.Combobox(rowf, textvariable=self.vars[key], font=FONT,
                                       values=[])  # 不预置：点「⟳ 获取」按当前 URL 实时拉取
                    self.model_box = ent
                    ent.grid(row=0, column=0, sticky="we")
                    self.fetch_btn = flat_btn(rowf, "⟳ 获取", self._fetch_models,
                                              fg=C["primary"], pady=4)
                    self.fetch_btn.grid(row=0, column=1, padx=(6, 0), sticky="e")
                    if editing:  # 编辑时把该源现有模型放入候选
                        cur = [x.strip() for x in
                               re.split(r"[,，]", self.vars["model"].get()) if x.strip()]
                        ent.config(values=cur)
                else:
                    ent = ttk.Entry(rowf, textvariable=self.vars[key], font=FONT)
                    ent.grid(row=0, column=0, sticky="we")
                if key == "api_key":
                    ent.config(show="" if self._key_visible else "•")
                    self.key_entry = ent
                    self.key_show = flat_btn(
                        rowf, "隐藏" if self._key_visible else "显示",
                        self._toggle_key, fg=C["primary"], pady=4)
                    self.key_show.grid(row=0, column=1, padx=(6, 0), sticky="e")
            sw = tk.Frame(card, bg=C["card"])
            sw.pack(anchor="w", padx=16, pady=(4, 0))
            self.enabled_pill = tk.Label(sw, text="", font=FONT_SM, padx=10, pady=3,
                                         cursor="hand2")
            self.enabled_pill.bind("<Button-1>", lambda e: self._toggle_enabled_form())
            self.enabled_pill.pack(side="left")
            self._render_enabled()
            bar = tk.Frame(card, bg=C["card"])
            bar.pack(fill="x", padx=16, pady=12)
            ttk.Button(bar, text="保存", style="Primary.TButton",
                       command=self._save_form).pack(side="left")
            ttk.Button(bar, text="取消", command=self.close_form).pack(
                side="left", padx=(10, 0))
            tk.Label(bar, text="API Key 保存在本机；请求时会发送给配置的中转源",
                     bg=C["card"], fg=C["light"], font=FONT_SM).pack(side="right")

        def _render_enabled(self):
            if self.enabled.get():
                self.enabled_pill.config(text="● 已启用（点击禁用）",
                                         bg=C["ok_light"], fg=C["ok"])
            else:
                self.enabled_pill.config(text="○ 已禁用（点击启用）",
                                         bg=C["seg"], fg=C["muted"])

        def _toggle_enabled_form(self):
            self.enabled.set(not self.enabled.get())
            self._render_enabled()

        def _toggle_key(self):
            self._key_visible = not self._key_visible
            self.key_entry.config(show="" if self._key_visible else "•")
            self.key_show.config(text="隐藏" if self._key_visible else "显示")

        # ---- 探测（原体检页能力，收进每张源卡） ----
        def _probe(self, i, label):
            cfg = self._cfg()
            if i >= len(cfg["sources"]):
                return
            s = cfg["sources"][i]
            label.config(text="探测中…", fg=C["muted"])
            self.app.set_status("正在探测「%s」…" % s["name"], "off")
            results = queue.Queue()
            headers = self.app.cfg_mod().api_headers(s["api_key"])

            def worker():
                try:
                    results.put(probe_relay(s["base_url"], headers))
                except Exception:  # noqa: BLE001
                    results.put(("✗ 探测异常", "探测发生异常，请重启应用后重试", False))

            def finish():
                if not label.winfo_exists():
                    return
                try:
                    text, detail, ok = results.get_nowait()
                except queue.Empty:
                    self.app.after(50, finish)
                    return
                label.config(text=text, fg=C["ok"] if ok else C["danger"])
                self.app.set_status("「%s」%s" % (s["name"], detail), "ok" if ok else "off")

            threading.Thread(target=worker, daemon=True).start()
            self.app.after(50, finish)

        def _ping(self, src):
            text, _detail, ok = probe_relay(src["base_url"],
                                             self.app.cfg_mod().api_headers(src["api_key"]))
            return text, C["ok"] if ok else C["danger"]

        # ---- 模型下拉实时获取（按当前表单 URL/Key 查 /v1/models） ----
        def _fetch_models(self):
            if self.fetch_btn.cget("text") == "获取中…":
                return
            base = self.vars["base_url"].get().strip().rstrip("/")
            key = self.vars["api_key"].get().strip()
            if not (base.startswith("http://") or base.startswith("https://")) or not key:
                self.app.set_status("请先填写 Base URL 和 API Key，再点「⟳ 获取」", "off")
                return
            self.fetch_btn.config(text="获取中…", fg=C["muted"])
            self.app.set_status("正在查询 %s/models …" % base, "off")
            results = queue.Queue()
            button, model_box = self.fetch_btn, self.model_box
            headers = self.app.cfg_mod().api_headers(key)

            def worker():
                try:
                    req = urllib.request.Request(
                        base + "/models", headers=headers)
                    with urllib.request.urlopen(req, timeout=15) as resp:
                        data = json.loads(resp.read().decode("utf-8", "replace"))
                    ids = [str(it["id"]) for it in (data.get("data") or [])
                           if isinstance(it, dict) and it.get("id")]
                    hits = [i for i in ids if IMAGE_HINT.search(i)]
                    ordered = hits + [i for i in ids if i not in hits]

                    results.put((ordered, len(hits), None))
                except Exception as e:  # noqa: BLE001
                    results.put((None, 0, str(e)))

            def finish():
                if not button.winfo_exists():
                    return
                try:
                    ordered, hit_count, error = results.get_nowait()
                except queue.Empty:
                    self.app.after(50, finish)
                    return
                button.config(text="⟳ 获取", fg=C["primary"])
                if error is not None:
                    self.app.set_status("获取失败：%s" % error, "off")
                else:
                    model_box.config(values=ordered)
                    if ordered:
                        self.app.set_status("获取到 %d 个模型（生图相关 %d 个），"
                                            "点模型下拉框选择" % (len(ordered), hit_count))
                    else:
                        self.app.set_status("该站未返回任何模型（检查地址/密钥）", "off")

            threading.Thread(target=worker, daemon=True).start()
            self.app.after(50, finish)

        def _clear_form(self):
            for v in self.vars.values():
                v.set("")
            self.enabled.set(True)
            self._key_visible = False

        def open_add(self):
            self.editing_index = None
            self._clear_form()
            self._form_visible = True
            self.refresh()
            self.scroll.cv.yview_moveto(0)
            self.app.set_status("新增模式：填写后点「保存」，可连续新增多条")

        def close_form(self):
            self.editing_index = None
            self._form_visible = False
            self.refresh()

        def _edit(self, i):
            cfg = self._cfg()
            if i >= len(cfg["sources"]):
                return
            s = cfg["sources"][i]
            m = self.app.cfg_mod()
            self.vars["name"].set(s["name"])
            self.vars["base_url"].set(s["base_url"])
            self.vars["api_key"].set(s["api_key"])
            self.vars["model"].set(", ".join(m.source_models(s)))
            self.vars["timeout"].set(str(s.get("timeout", "")))
            self.enabled.set(bool(s.get("enabled", True)))
            self._key_visible = False
            self.editing_index = i
            self._form_visible = True
            self.refresh()
            self.scroll.cv.yview_moveto(0)
            self.app.set_status("正在编辑「%s」，保存或取消后生效" % s["name"])

        def _form_source(self):
            m = self.app.cfg_mod()
            models = [x.strip() for x in re.split(r"[,，]", self.vars["model"].get())
                      if x.strip()]
            timeout = self.vars["timeout"].get().strip()
            return m.normalize_source(
                self.vars["name"].get(), self.vars["base_url"].get(),
                self.vars["api_key"].get(), models,
                enabled=self.enabled.get(), timeout=int(timeout) if timeout else None)

        def _save_form(self):
            try:
                src = self._form_source()
            except (RuntimeError, ValueError) as e:
                messagebox.showerror("无法保存", str(e), parent=self)
                return
            cfg = self._cfg()
            if self.editing_index is None:
                if self.app.cfg_mod().find_source(cfg, src["name"]):
                    messagebox.showerror("无法添加", "已存在同名源「%s」" % src["name"],
                                         parent=self)
                    return
                cfg["sources"].append(src)
                msg = "已添加「%s」并保存（优先级第 %d）" % (src["name"],
                                                          len(cfg["sources"]))
            else:
                for k, s in enumerate(cfg["sources"]):
                    if k != self.editing_index and s["name"] == src["name"]:
                        messagebox.showerror("无法保存", "已存在同名源「%s」" % src["name"],
                                             parent=self)
                        return
                old_name = cfg["sources"][self.editing_index]["name"]
                cfg["sources"][self.editing_index] = src
                if cfg.get("prefer") == old_name:
                    cfg["prefer"] = src["name"]  # 首选跟随改名
                msg = "已更新「%s」并保存" % src["name"]
            self._save(cfg)
            self.editing_index = None
            self._form_visible = False
            self.refresh()
            self.app.set_status(msg)
            self.app.refresh_state()

        def _toggle_enabled_at(self, i):
            cfg = self._cfg()
            if i >= len(cfg["sources"]):
                return
            s = cfg["sources"][i]
            s["enabled"] = not s.get("enabled", True)
            self._save(cfg)
            self.refresh()
            self.app.set_status("「%s」已%s" % (s["name"],
                                               "参与生图" if s["enabled"] else "禁用（不再尝试）"))
            self.app.refresh_state()

        def _remove(self, i):
            cfg = self._cfg()
            if i >= len(cfg["sources"]):
                return
            name = cfg["sources"][i]["name"]
            if not messagebox.askyesno("确认删除", "删除源「%s」？" % name, parent=self):
                return
            del cfg["sources"][i]
            if cfg.get("prefer") == name:
                cfg.pop("prefer", None)  # 首选源被删：回退首个启用源
            self._save(cfg)
            if self.editing_index == i:
                self.editing_index = None
                self._form_visible = False
            self.refresh()
            self.app.set_status("已删除「%s」" % name)
            self.app.refresh_state()

        def _move(self, i, delta):
            cfg = self._cfg()
            j = i + delta
            if not (0 <= j < len(cfg["sources"])):
                return
            cfg["sources"][i], cfg["sources"][j] = cfg["sources"][j], cfg["sources"][i]
            if self.editing_index == i:
                self.editing_index = j
            elif self.editing_index == j:
                self.editing_index = i
            self._save(cfg)
            self.refresh()
            self.app.set_status("已调整后备顺序：「%s」→ 第 %d 位"
                                % (cfg["sources"][j]["name"], j + 1))
            self.app.refresh_state()

        def _make_prefer(self, i):
            """把第 i 个源设为★首选（记名到 cfg["prefer"]，不改动卡片顺序；
            禁用源会同时恢复参与生图——首选必须参与）。"""
            cfg = self._cfg()
            if i >= len(cfg["sources"]):
                return
            src = cfg["sources"][i]
            if not src.get("enabled", True):
                src["enabled"] = True
            cfg["prefer"] = src["name"]
            self._save(cfg)
            self.refresh()
            self.app.set_status("「%s」已设为★首选（生图先试它，卡片顺序不变）"
                                % src["name"])
            self.app.refresh_state()

    # ---- 主窗口：ccswitch 式左侧导航栏 + 页面容器 ----
    NAV = [("dashboard", "⏻", "Agent 接入"), ("sources", "⚙", "中转源")]

    class App(tk.Tk):
        def __init__(self):
            super().__init__()
            self.title("IMG Switch")
            self.geometry("940x680")
            self.minsize(880, 640)
            self.configure(bg=C["bg"])
            self.update_idletasks()
            sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
            self.geometry("+%d+%d" % (max(0, (sw - 940) // 2),
                                      max(0, (sh - 680) // 2 - 20)))
            self._cfg_mod_cache = None
            self._current_page = None
            self._build()
            upgrade_failures = sync_connected_skills()
            self.refresh_state()
            self.show_page("dashboard")
            if upgrade_failures:
                messagebox.showwarning(
                    "Agent 引擎更新失败",
                    "无法更新以下已接入的 Agent：%s。旧引擎已保留；"
                    "请关闭相关 Agent 后重新启动 IMG Switch，软件会再次尝试。"
                    % "、".join(host for host, _error in upgrade_failures), parent=self)

        # ---- 结构 ----
        def _build(self):
            st = ttk.Style(self)
            st.theme_use("clam")
            st.configure(".", background=C["bg"], foreground=C["text"], font=FONT)
            st.configure("TButton", background=C["card"], foreground=C["text"],
                         borderwidth=1, focusthickness=1, focuscolor=C["line"],
                         padding=(10, 6), font=FONT)
            st.map("TButton", background=[("pressed", "#e5e7eb"),
                                          ("active", C["seg"])])
            st.configure("Primary.TButton", background=C["primary"],
                         foreground="#ffffff", borderwidth=0,
                         padding=(14, 7), font=FONT_BOLD)
            st.map("Primary.TButton",
                   background=[("disabled", "#fdba74"),
                               ("pressed", C["primary_dark"]),
                               ("active", C["primary_dark"])])
            # 输入框 / 下拉框：白底 + 细描边，聚焦时橙色描边
            field = dict(fieldbackground="#ffffff", bordercolor=C["line"],
                         lightcolor=C["line"], darkcolor=C["line"])
            st.configure("TEntry", padding=6, **field)
            st.map("TEntry",
                   bordercolor=[("focus", C["primary"])],
                   lightcolor=[("focus", C["primary"])],
                   darkcolor=[("focus", C["primary"])])
            st.configure("TCombobox", padding=6, arrowsize=12,
                         arrowcolor=C["muted"], **field)
            st.map("TCombobox",
                   fieldbackground=[("readonly", "#ffffff"), ("focus", "#ffffff")],
                   bordercolor=[("focus", C["primary"])],
                   lightcolor=[("focus", C["primary"])],
                   darkcolor=[("focus", C["primary"])])
            # 滚动条：浅灰滑块、无箭头浮雕（内容不超屏时整体隐藏）
            st.configure("TScrollbar", troughcolor=C["bg"], background="#c9ced6",
                         borderwidth=0, arrowsize=11, relief="flat",
                         lightcolor=C["bg"], darkcolor=C["bg"])
            st.map("TScrollbar",
                   background=[("active", "#b6bcc6"), ("pressed", "#a8aeb8")])
            self.bind_all("<MouseWheel>", self._wheel)

            bar = tk.Frame(self, bg=C["card"], highlightthickness=1,
                           highlightbackground=C["line"])
            bar.pack(side="bottom", fill="x")
            self.dot = tk.Label(bar, text="●", bg=C["card"], fg=C["ok"], font=FONT_SM)
            self.dot.pack(side="left", padx=(16, 6), pady=8)
            self.status = tk.StringVar(value="就绪")
            tk.Label(bar, textvariable=self.status, bg=C["card"], fg=C["text"],
                     font=FONT_SM).pack(side="left")
            tk.Label(bar, text="统一配置：" + appdata_config(), bg=C["card"],
                     fg=C["light"], font=FONT_SM).pack(side="right", padx=16)

            side = tk.Frame(self, bg=C["side"], width=190, highlightthickness=1,
                            highlightbackground=C["line"])
            side.pack(side="left", fill="y")
            side.pack_propagate(False)
            logo = tk.Frame(side, bg=C["side"])
            logo.pack(fill="x", padx=16, pady=(16, 14))
            mark = tk.Canvas(logo, width=30, height=30, bg=C["side"],
                             highlightthickness=0)
            rrect(mark, 1, 1, 29, 29, 8, fill=C["primary"], outline="")
            rrect(mark, 7, 7, 21, 19, 3, fill="#ffe7d2", outline="")
            rrect(mark, 10, 11, 24, 23, 3, fill="#ffffff", outline="")
            mark.create_polygon(12, 21, 16, 16, 19, 19, 21, 17, 24, 21,
                                fill=C["primary"], outline="")
            mark.pack(side="left", padx=(0, 10))
            tbox = tk.Frame(logo, bg=C["side"])
            tbox.pack(side="left")
            tk.Label(tbox, text="IMG Switch", bg=C["side"], fg=C["text"],
                     font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w")
            tk.Label(tbox, text="img-switch", bg=C["side"], fg=C["light"],
                     font=("Microsoft YaHei UI", 8)).pack(anchor="w")

            self.nav_buttons = {}
            for i, (key, icon, label) in enumerate(NAV):
                b = tk.Label(side, text=" %s  %s" % (icon, label), anchor="w",
                             bg=C["side"], fg=C["text"], font=FONT, padx=14, pady=9,
                             cursor="hand2")
                b.pack(fill="x", padx=8, pady=(8 if i == 0 else 2, 0))
                b.bind("<Button-1>", lambda e, k=key: self.show_page(k))
                self.nav_buttons[key] = b

            bottom = tk.Frame(side, bg=C["side"])
            bottom.pack(side="bottom", fill="x", pady=(0, 10))
            tk.Frame(bottom, bg=C["line"], height=1).pack(fill="x", pady=(0, 8))
            self.side_state = tk.Label(bottom, text="", bg=C["side"], fg=C["muted"],
                                       font=FONT_SM, anchor="w", padx=22)
            self.side_state.pack(anchor="w")

            self.container = tk.Frame(self, bg=C["bg"])
            self.container.pack(side="left", fill="both", expand=True)
            self.container.grid_rowconfigure(0, weight=1)
            self.container.grid_columnconfigure(0, weight=1)
            self.pages = {
                "dashboard": DashboardPage(self.container, self),
                "sources": SourcesPage(self.container, self),
            }
            for page in self.pages.values():
                page.grid(row=0, column=0, sticky="nsew")

        def _wheel(self, e):
            box = getattr(self._current_page, "scroll", None)
            if box is not None:
                box.cv.yview_scroll(-int(e.delta / 120), "units")

        # ---- 公共 ----
        def cfg_mod(self):
            if self._cfg_mod_cache is None:
                self._cfg_mod_cache = load_engine_module(
                    "failovercfg.py", "_fig_app_failovercfg")
            return self._cfg_mod_cache

        def read_sources(self):
            try:
                cfg = self.cfg_mod().load_config(appdata_config())
            except RuntimeError:
                return []
            return (cfg or {}).get("sources", [])

        def set_status(self, text, kind="ok"):
            self.status.set(text)
            self.dot.config(fg=C["ok"] if kind == "ok" else C["light"])

        def show_page(self, key):
            # 从「中转源」切走时收起编辑/新增表单，回来始终是列表态
            if self._current_page is self.pages.get("sources") and key != "sources":
                sp = self.pages["sources"]
                sp.editing_index = None
                sp._form_visible = False
            self.pages[key].tkraise()
            self._current_page = self.pages[key]
            for k, b in self.nav_buttons.items():
                if k == key:
                    b.config(bg=C["select"], fg=C["text"])
                else:
                    b.config(bg=C["side"], fg=C["muted"])
            if key in ("dashboard", "sources"):
                self.pages[key].refresh()

        def refresh_state(self):
            self.pages["dashboard"].refresh()  # 同步 Agent 开关 + 侧栏状态
            self.pages["sources"].refresh()
            hooked = sum(1 for h, _l, _r, a in agent_targets()
                         if a and host_hooked(h))
            self.set_status("已接入 %d 个 Agent ｜ 已配置 %d 个中转源 ｜ 故障转移%s"
                            % (hooked, len(self.read_sources()),
                               "开" if failover_now() else "关"))

        # ---- 故障转移开关（写 config.json 顶层 failover，引擎读取） ----
        def set_failover(self, on):
            try:
                cfg = self.cfg_mod().load_config(appdata_config()) \
                    or {"version": 1, "sources": []}
            except RuntimeError:
                cfg = {"version": 1, "sources": []}
            cfg["failover"] = bool(on)
            self.cfg_mod().save_config(cfg, appdata_config())
            self.pages["sources"].update_failover_ui()
            if on:
                self.set_status("故障转移已开启：失败自动切换下一个源/模型")
            else:
                self.set_status("故障转移已关闭：只用★首选源，失败直接报错")

        # ---- Agent 按需接入 ----
        def toggle_host(self, host, want_on):
            label = dict(AGENT_HOSTS).get(host, host)
            try:
                set_host_enabled(host, want_on)
                if want_on:
                    installed = install_skill([host])
                    self.set_status("已接入 %s（%d 处安装）" % (label, len(installed)))
                else:
                    removed = remove_skill([host])
                    self.set_status("已断开 %s（%d 处移除，配置保留）"
                                    % (label, len(removed)))
            except Exception as e:  # noqa: BLE001
                messagebox.showerror("操作失败", str(e))
            self.refresh_state()  # 失败时把开关拨回真实状态

        def connect_all(self):
            try:
                for host, _l, _r, active in agent_targets():
                    if active:
                        set_host_enabled(host, True)
                installed = install_skill()
                self.set_status("全部接入：安装到 %d 个 Agent 目录" % len(installed))
            except Exception as e:  # noqa: BLE001
                messagebox.showerror("操作失败", str(e))
            self.refresh_state()

        def disconnect_all(self):
            try:
                removed = remove_skill()  # 全部移除，但不改动「排除名单」
                self.set_status("全部断开：移除 %d 处安装（配置保留）" % len(removed))
            except Exception as e:  # noqa: BLE001
                messagebox.showerror("操作失败", str(e))
            self.refresh_state()

    app = App()
    if os.environ.get("FAILOVER_IMAGEGEN_APP_SMOKE"):
        app.update()
        app.update_idletasks()
        app.destroy()
        print("app smoke ok（窗口创建/刷新/销毁通过）")
        return 0
    if os.environ.get("FAILOVER_IMAGEGEN_APP_SELFTEST"):
        # 自测：真实构建界面后断言页面/开关/卡片/新增流程（供 app_tests.py 验证）
        app.update()
        app.update_idletasks()
        assert set(app.pages) == {"dashboard", "sources"}, \
            "页面缺失：%s" % list(app.pages)
        dp = app.pages["dashboard"]
        for key in ("dashboard", "sources", "dashboard"):
            app.show_page(key)
            app.update()
            page = app.pages[key]
            header = page.winfo_children()[0]
            assert header.winfo_ismapped() and header.winfo_height() > 1, \
                "%s 页面标题不可见: page=%s header=%s" % (
                    key, page.winfo_geometry(), header.winfo_geometry())
            visible = app.winfo_containing(header.winfo_rootx() + 5,
                                           header.winfo_rooty() + 5)
            assert visible is not None and str(visible).startswith(str(page) + "."), \
                "%s 页面被其他页面遮挡" % key
            assert all(p.winfo_ismapped() for p in app.pages.values()), \
                "切换页面时不应卸载其他页面"
        active_n = sum(1 for h, _l, _r, a in agent_targets() if a)
        assert len(dp.cover_rows) == active_n, \
            "Agent 卡片数不符（只显示本机已安装的）：%d vs %d" % (
                len(dp.cover_rows), active_n)
        assert len(dp.agent_switches) == active_n, \
            "Agent 独立开关数不符：%d" % len(dp.agent_switches)
        fai_before = failover_now()
        app.set_failover(not fai_before)
        assert failover_now() == (not fai_before), "故障转移开关未写入配置"
        app.set_failover(fai_before)
        assert failover_now() == fai_before, "故障转移开关未恢复原值"
        sp = app.pages["sources"]
        assert getattr(sp, "fai_switch", None) is not None, "源页故障转移开关缺失"
        srcs = app.read_sources()
        assert sp.src_count() == len(srcs), "中转源卡片数不符：%d vs %d" % (
            sp.src_count(), len(srcs))
        before = len(srcs)
        sp.open_add()
        assert sp.form_open(), "新增表单未展开"
        assert getattr(sp, "fetch_btn", None) is not None, "模型「⟳ 获取」按钮缺失"
        sp.vars["name"].set("selftest-src")
        sp.vars["base_url"].set("https://selftest.example.com/v1")
        sp.vars["api_key"].set("sk-selftest")
        sp.vars["model"].set("m-x")
        sp._save_form()
        after = app.read_sources()
        assert len(after) == before + 1 and any(
            s["name"] == "selftest-src" for s in after), "内嵌新增源未写入配置"
        assert not sp.form_open(), "保存后表单未收起"
        assert sp.src_count() == len(after), "保存后卡片数与配置不符"
        if before > 0:  # 已有源时验证「设为首选」只标记、不改顺序
            first_name = after[0]["name"]
            sp._make_prefer(before)
            doc = app.cfg_mod().load_config(appdata_config()) or {}
            after2 = app.read_sources()
            assert doc.get("prefer") == "selftest-src", "prefer 标记未写入配置"
            assert [s["name"] for s in after2] == [s["name"] for s in after], \
                "设为首选不应改动卡片顺序"
        # 清理自测数据，避免污染真实配置
        try:
            cfg = app.cfg_mod().load_config(appdata_config()) or {"sources": []}
        except RuntimeError:
            cfg = {"sources": []}
        cfg["sources"] = [s for s in cfg["sources"] if s["name"] != "selftest-src"]
        app.cfg_mod().save_config(cfg, appdata_config())
        final = app.read_sources()
        assert not any(s["name"] == "selftest-src" for s in final), "自测源未清理干净"
        # 连接失败时必须恢复按钮并显示原因，不能在异步回调里再次抛错。
        import socket
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        errors = []
        app.report_callback_exception = lambda typ, value, tb: errors.append(value)
        sp.open_add()
        sp.vars["base_url"].set("http://127.0.0.1:%d/v1" % port)
        sp.vars["api_key"].set("selftest-key")
        sp._fetch_models()
        def wait_fetch():
            if errors or sp.fetch_btn.cget("text") != "获取中…":
                app.quit()
            else:
                app.after(20, wait_fetch)
        app.after(20, wait_fetch)
        timeout_id = app.after(10000, app.quit)
        app.mainloop()
        app.after_cancel(timeout_id)
        assert not errors, "模型获取失败回调报错: %s" % errors
        assert sp.fetch_btn.cget("text") == "⟳ 获取", \
            "模型获取失败后按钮未恢复: %s / %s" % (sp.fetch_btn.cget("text"), app.status.get())
        assert app.status.get().startswith("获取失败："), "模型获取失败原因未显示"
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        class StrictRelay(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                if not self.headers.get("User-Agent", "").startswith("IMG-Switch/"):
                    self.send_response(403)
                    self.end_headers()
                    return
                body = b'{"data":[{"id":"test-image"}]}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        server = ThreadingHTTPServer(("127.0.0.1", 0), StrictRelay)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            base = "http://127.0.0.1:%d/v1" % server.server_port
            assert sp._ping({"base_url": base, "api_key": "test-key"})[0].startswith("✓"), \
                "中转站探测未发送正确的客户端标识"
            sp.vars["base_url"].set(base)
            sp._fetch_models()
            app.after(20, wait_fetch)
            timeout_id = app.after(10000, app.quit)
            app.mainloop()
            app.after_cancel(timeout_id)
            assert not errors, "模型获取回调报错: %s" % errors
            assert "test-image" in sp.model_box.cget("values"), \
                "未获取到模型列表: %s" % app.status.get()
        finally:
            server.shutdown()
            server.server_close()
        app.destroy()
        print("app selftest ok（2 页面/Agent 卡 %d 张/源页故障转移/源卡 %d 张/设为首选/内嵌新增）"
              % (active_n, len(after)))
        return 0
    app.mainloop()
    return 0


def _child_setup_gui():
    """子进程入口：运行引擎独立配置界面（保留给绿色版/脚本调用）。"""
    mod = load_engine_module("setup.py", "_fig_engine_setup")
    return mod.main(["gui"])


def _child_tool():
    """子进程入口：运行引擎命令行，输出同时写入 FAILOVER_IMAGEGEN_CHILD_OUT 指定文件。"""
    out_path = os.environ.get("FAILOVER_IMAGEGEN_CHILD_OUT")
    out_file = None

    class _Tee:
        def __init__(self, *streams):
            self.streams = streams

        def write(self, s):
            for st in self.streams:
                try:
                    st.write(s)
                except Exception:
                    pass

        def flush(self):
            for st in self.streams:
                try:
                    st.flush()
                except Exception:
                    pass

    if out_path:
        out_file = open(out_path, "w", encoding="utf-8", errors="replace")
        sys.stdout = _Tee(sys.stdout, out_file)
    try:
        mod = load_engine_module("failover_imagegen_tool.py", "_fig_engine_tool")
        return mod.main(sys.argv[2:])
    finally:
        if out_file:
            try:
                out_file.close()
            except Exception:
                pass


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    # 兜底：windowed exe 下未捕获异常会弹模态错误框把进程挂死，这里统一转成退出码
    try:
        cmd = (argv or sys.argv[1:] or ["gui"])[0]
        if cmd == "on":
            return cmd_on()
        if cmd == "off":
            return cmd_off()
        if cmd == "status":
            return cmd_status()
        if cmd == "__setup_gui":  # 隐藏子命令
            return _child_setup_gui()
        if cmd == "__tool":  # 隐藏子命令
            return _child_tool()
        if cmd == "gui":
            return cmd_gui()
        print(__doc__)
        return 2
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
