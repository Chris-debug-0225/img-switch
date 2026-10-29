#!/usr/bin/env python3
"""failover-imagegen-app 测试：临时 HOME 隔离下验证开关、配置迁移与 GUI。

全部子进程真实执行 app.py CLI 与引擎脚本，不触碰真实用户目录。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(HERE, "app.py")
PY = sys.executable
RESULTS = []
HOMES = []


def utf8_streams():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def test_env(home):
    env = dict(os.environ)
    env["FAILOVER_IMAGEGEN_APP_HOME"] = home
    env["HOME"] = home
    env["USERPROFILE"] = home
    env["APPDATA"] = os.path.join(home, "AppData", "Roaming")
    env["XDG_CONFIG_HOME"] = os.path.join(home, ".config")
    env["PYTHONIOENCODING"] = "utf-8"
    # Do not override config: exercise the platform-specific fallback.
    env.pop("FAILOVER_IMAGEGEN_TOOL_CONFIG", None)
    return env


def config_dir(home):
    base = os.path.join(home, "AppData", "Roaming") if os.name == "nt" else os.path.join(home, ".config")
    return os.path.join(base, "failover-imagegen-tool")


def run(args, home, extra_env=None, timeout=120):
    env = test_env(home)
    env.update(extra_env or {})
    return subprocess.run([PY, APP] + args, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env, timeout=timeout)


def record(name, fn):
    try:
        detail = fn()
        RESULTS.append((name, True, detail or "ok"))
        print("  PASS %s — %s" % (name, detail))
    except AssertionError as e:
        RESULTS.append((name, False, str(e)))
        print("  FAIL %s: %s" % (name, e))
    except Exception as e:  # noqa: BLE001
        RESULTS.append((name, False, "%s: %s" % (type(e).__name__, e)))
        print("  ERROR %s: %s: %s" % (name, type(e).__name__, e))


def make_home(with_hosts=(".codex", ".claude")):
    home = tempfile.mkdtemp(prefix="figapp_")
    HOMES.append(home)
    for h in with_hosts:
        os.makedirs(os.path.join(home, h), exist_ok=True)
    return home


def t1_initial_status_off():
    home = make_home()
    p = run(["status"], home)
    assert p.returncode == 0 and "已关闭" in p.stdout, p.stdout + p.stderr
    return "初始状态=已关闭"


def t2_on_installs_to_existing_agents():
    home = make_home(with_hosts=(".codex", ".claude"))
    p = run(["on"], home)
    assert p.returncode == 0, p.stdout + p.stderr
    for host in (".agents", ".codex", ".claude"):
        dst = os.path.join(home, host, "skills", "failover-imagegen-tool")
        assert os.path.isfile(os.path.join(dst, "SKILL.md")), "缺少安装: " + dst
        assert os.path.isfile(os.path.join(dst, ".installed-by-app")), "缺少标记: " + dst
        assert not os.path.exists(os.path.join(dst, "config.json")), "安装副本不应带配置: " + dst
    zcode = os.path.join(home, ".zcode")
    assert not os.path.exists(zcode), ".zcode 不存在时不应被创建"
    s = run(["status"], home)
    assert "已开启" in s.stdout and s.stdout.count("已接入") >= 3, s.stdout
    return "on 安装到 .agents/.codex/.claude（.zcode 未存在则不建）"


def t3_local_config_migrated_to_appdata():
    home = make_home(with_hosts=(".codex",))
    dst = os.path.join(home, ".codex", "skills", "failover-imagegen-tool")
    os.makedirs(dst, exist_ok=True)
    with open(os.path.join(dst, "SKILL.md"), "w", encoding="utf-8") as f:
        f.write("---\nname: failover-imagegen-tool\n---\n")
    with open(os.path.join(dst, ".installed-by-app"), "w", encoding="utf-8") as f:
        f.write("installed-by failover-imagegen-app\n")
    with open(os.path.join(dst, "config.json"), "w", encoding="utf-8") as f:
        json.dump({"version": 1, "sources": [
            {"name": "old-local", "base_url": "http://127.0.0.1:9/v1",
             "api_key": "sk-old", "model": "m"}]}, f)
    p = run(["on"], home)
    assert p.returncode == 0, p.stdout + p.stderr
    cfg = os.path.join(config_dir(home), "config.json")
    assert os.path.isfile(cfg), "本地配置未迁移到统一配置目录"
    data = json.load(open(cfg, encoding="utf-8"))
    assert data["sources"][0]["name"] == "old-local", data
    return "已安装点的本地 config.json 迁入统一配置且安装副本不带密钥"


def t4_engine_appdata_fallback():
    home = make_home(with_hosts=(".codex",))
    run(["on"], home)  # 先装好（此时无配置）
    # 写入平台统一配置
    cfg = os.path.join(config_dir(home), "config.json")
    os.makedirs(os.path.dirname(cfg), exist_ok=True)
    with open(cfg, "w", encoding="utf-8") as f:
        json.dump({"version": 1, "sources": [
            {"name": "appdata-src", "base_url": "http://127.0.0.1:9/v1",
             "api_key": "sk-a", "model": "m"}]}, f)
    engine = os.path.join(home, ".codex", "skills", "failover-imagegen-tool",
                          "scripts", "failover_imagegen_tool.py")
    env = test_env(home)
    p = subprocess.run([PY, engine, "--list-sources"], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=env, timeout=60)
    assert p.returncode == 0 and "appdata-src" in p.stdout, p.stdout + p.stderr
    return "安装副本无本地配置时自动回退统一配置"


def t5_off_removes_and_keeps_config():
    home = make_home(with_hosts=(".codex", ".claude"))
    run(["on"], home)
    cfg = os.path.join(config_dir(home), "config.json")
    os.makedirs(os.path.dirname(cfg), exist_ok=True)
    with open(cfg, "w", encoding="utf-8") as f:
        json.dump({"version": 1, "sources": [
            {"name": "keep-me", "base_url": "http://x/v1", "api_key": "sk-k", "model": "m"}]}, f)
    p = run(["off"], home)
    assert p.returncode == 0, p.stdout + p.stderr
    for host in (".agents", ".codex", ".claude"):
        dst = os.path.join(home, host, "skills", "failover-imagegen-tool")
        assert not os.path.exists(dst), "off 后仍残留: " + dst
    assert os.path.isfile(cfg), "off 不应删除统一配置"
    s = run(["status"], home)
    assert "已关闭" in s.stdout, s.stdout
    on_again = run(["on"], home)
    assert on_again.returncode == 0
    dst = os.path.join(home, ".agents", "skills", "failover-imagegen-tool")
    assert os.path.isdir(dst)
    return "off 全部移除且统一配置保留；重新 on 恢复"


def t6_off_skips_foreign_dir():
    home = make_home(with_hosts=(".codex",))
    dst = os.path.join(home, ".codex", "skills", "some-other-skill")
    os.makedirs(dst, exist_ok=True)
    open(os.path.join(dst, "SKILL.md"), "w", encoding="utf-8").write("name: other\n")
    run(["on"], home)
    run(["off"], home)
    assert os.path.isdir(dst), "off 不应误删无关技能目录"
    return "off 只移除本 skill，不碰其他技能目录"


def t7_gui_smoke():
    home = make_home()
    p = run(["gui"], home, timeout=60, extra_env={"FAILOVER_IMAGEGEN_APP_SMOKE": "1"})
    assert p.returncode == 0 and "app smoke ok" in p.stdout, \
        "rc=%d out=%s err=%s" % (p.returncode, p.stdout, p.stderr)
    return "App GUI 窗口创建/刷新/销毁冒烟通过"


def t8_child_tool_delegation():
    """GUI 按钮「模型发现/源体检」的链路：__tool 子命令回传输出（管道/文件双通道）。"""
    home = make_home(with_hosts=(".codex",))
    run(["on"], home)
    cfg = os.path.join(config_dir(home), "config.json")
    os.makedirs(os.path.dirname(cfg), exist_ok=True)
    with open(cfg, "w", encoding="utf-8") as f:
        json.dump({"version": 1, "sources": [
            {"name": "btn-src", "base_url": "http://127.0.0.1:9/v1",
             "api_key": "sk-b", "model": "m"}]}, f)
    p = run(["__tool", "--list-sources"], home, timeout=90)
    assert p.returncode == 0, p.stdout + p.stderr
    assert "btn-src" in (p.stdout or ""), "输出未回传: %r" % (p.stdout or "")
    return "__tool 子命令执行引擎并回传输出"


def t9_child_setup_gui_delegation():
    """GUI 按钮「配置中转站」的链路：__setup_gui 子命令能拉起引擎配置界面（冒烟）。"""
    home = make_home()
    p = run(["__setup_gui"], home, timeout=60,
            extra_env={"FAILOVER_IMAGEGEN_TOOL_GUI_SMOKE": "1"})
    assert p.returncode == 0 and "gui smoke ok" in (p.stdout or ""), \
        "rc=%d out=%s err=%s" % (p.returncode, p.stdout, p.stderr)
    return "__setup_gui 子命令拉起引擎配置界面"


def t10_gui_selftest_with_sources():
    """界面自测：故障转移开关/Agent 开关/中转源卡片在已配置环境下正确渲染。"""
    home = make_home(with_hosts=(".codex",))
    appdata = config_dir(home)
    os.makedirs(appdata, exist_ok=True)
    with open(os.path.join(appdata, "config.json"), "w", encoding="utf-8") as f:
        json.dump({"version": 1, "sources": [
            {"name": "Alpha", "base_url": "http://a.example/v1", "api_key": "sk-1",
             "model": "m1"}, {"name": "Beta", "base_url": "http://b.example/v1",
             "api_key": "sk-2", "models": ["m2", "m3"]}]}, f)
    p = run(["gui"], home, timeout=60, extra_env={"FAILOVER_IMAGEGEN_APP_SELFTEST": "1"})
    assert p.returncode == 0 and "app selftest ok（2 页面/Agent 卡 2 张/源页故障转移/源卡 3 张/设为首选/内嵌新增）" in (p.stdout or ""), \
        "rc=%d out=%s err=%s" % (p.returncode, p.stdout, p.stderr)
    return "界面自测：2 页面+Agent 卡 2 张（未装的不显示）+源页故障转移+源卡 3 张+设为首选+内嵌新增"


def t11_agent_selection():
    """按需接入：app-settings 排除 .codex 后 on 跳过它；解除排除后恢复安装。"""
    home = make_home(with_hosts=(".codex", ".claude"))
    appdata = config_dir(home)
    os.makedirs(appdata, exist_ok=True)
    with open(os.path.join(appdata, "app-settings.json"), "w", encoding="utf-8") as f:
        json.dump({"disabled_hosts": [".codex"]}, f)
    p = run(["on"], home)
    assert p.returncode == 0 and "已按设置排除" in p.stdout, p.stdout + p.stderr
    assert os.path.isdir(os.path.join(home, ".agents", "skills", "failover-imagegen-tool")), \
        ".agents 应安装"
    assert os.path.isdir(os.path.join(home, ".claude", "skills", "failover-imagegen-tool")), \
        ".claude 应安装"
    assert not os.path.exists(os.path.join(home, ".codex", "skills", "failover-imagegen-tool")), \
        ".codex 应被排除"
    s = run(["status"], home)
    assert "已按设置排除" in s.stdout, s.stdout
    with open(os.path.join(appdata, "app-settings.json"), "w", encoding="utf-8") as f:
        json.dump({"disabled_hosts": []}, f)
    p = run(["on"], home, extra_env={"FAILOVER_IMAGEGEN_APP_FORCE": "1"})
    assert p.returncode == 0, p.stdout + p.stderr
    assert os.path.isdir(os.path.join(home, ".codex", "skills", "failover-imagegen-tool")), \
        "解除排除后 .codex 应安装"
    g = run(["gui"], home, timeout=60, extra_env={"FAILOVER_IMAGEGEN_APP_SMOKE": "1"})
    assert g.returncode == 0, g.stdout + g.stderr
    return "app-settings 排除名单生效（on 跳过 / 解除后安装 / 状态提示 / GUI 冒烟）"


def main():
    utf8_streams()
    print("=" * 62)
    print("failover-imagegen-app 测试（临时 HOME 隔离，不触碰真实目录）")
    print("=" * 62)
    tests = [
        ("A1 初始状态=关闭", t1_initial_status_off),
        ("A2 on 安装到全部已存在 Agent", t2_on_installs_to_existing_agents),
        ("A3 本地配置迁移", t3_local_config_migrated_to_appdata),
        ("A4 引擎统一配置回退", t4_engine_appdata_fallback),
        ("A5 off 移除+配置保留+重开恢复", t5_off_removes_and_keeps_config),
        ("A6 off 不误删无关目录", t6_off_skips_foreign_dir),
        ("A7 GUI 冒烟", t7_gui_smoke),
        ("A8 按钮链路-引擎命令回传", t8_child_tool_delegation),
        ("A9 按钮链路-配置界面拉起", t9_child_setup_gui_delegation),
        ("A10 界面自测（故障转移/Agent开关/源卡）", t10_gui_selftest_with_sources),
        ("A11 Agent 按需接入", t11_agent_selection),
    ]
    for name, fn in tests:
        record(name, fn)
    for h in HOMES:
        shutil.rmtree(h, ignore_errors=True)
    HOMES.clear()
    ok = sum(1 for _, passed, _ in RESULTS if passed)
    print("-" * 62)
    print("结果：%d/%d 通过" % (ok, len(RESULTS)))
    return 0 if ok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
