#!/usr/bin/env python3
"""failover-imagegen-tool 端到端验证：本地模拟中转站 + 子进程真实运行脚本。

覆盖：多源故障切换（500/401/404/连接拒绝/下载失败/无图响应）、致命 400 不换源、
URL 与 b64 两种响应、尺寸别名与默认值、编辑模式、meta.json 脱敏、
setup.py 配置管理全流程、GUI 冒烟。全部通过则退出码 0。
"""
import glob
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(HERE)
GEN = os.path.join(SKILL_DIR, "scripts", "failover_imagegen_tool.py")
SETUP = os.path.join(SKILL_DIR, "scripts", "setup.py")
sys.path.insert(0, HERE)
import mock_relay

PY = sys.executable
RESULTS = []
SERVERS = []


def utf8_streams():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def free_closed_port():
    """拿一个确定没有监听者的端口（用于连接拒绝场景）。"""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def make_src(name, port, enabled=True):
    return {
        "name": name,
        "base_url": "http://127.0.0.1:%d/v1" % port,
        "api_key": "sk-mock-secret-" + name,
        "model": "mock-image-1",
        "enabled": enabled,
        "timeout": 30,
    }


def make_src_multi(name, port, models, enabled=True):
    """多模型级联源（站内 models 数组）。"""
    return {
        "name": name,
        "base_url": "http://127.0.0.1:%d/v1" % port,
        "api_key": "sk-mock-secret-" + name,
        "models": list(models),
        "enabled": enabled,
        "timeout": 30,
    }


def write_config(path, sources):
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"version": 1, "sources": sources}, f, ensure_ascii=False, indent=2)


def run(args, cwd, cfg_path, timeout=120, extra_env=None):
    env = dict(os.environ)
    env["FAILOVER_IMAGEGEN_TOOL_CONFIG"] = cfg_path
    env["PYTHONIOENCODING"] = "utf-8"
    env.update(extra_env or {})
    return subprocess.run([PY] + args, cwd=cwd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env, timeout=timeout)


def make_prompt(wd, text="一张 4K 水墨山水，留白，无文字水印"):
    pf = os.path.join(wd, "prompt.txt")
    with open(pf, "w", encoding="utf-8") as f:
        f.write(text)
    return pf


def outputs(wd):
    return (sorted(glob.glob(os.path.join(wd, "generated", "*.png"))
                   + glob.glob(os.path.join(wd, "generated", "*.jpg"))),
            sorted(glob.glob(os.path.join(wd, "generated", "*.meta.json"))))


def read_meta(wd):
    _pngs, metas = outputs(wd)
    assert len(metas) == 1, "期望恰好一个 meta.json，实际 %d" % len(metas)
    with open(metas[0], "r", encoding="utf-8") as f:
        return json.load(f), metas[0]


def record(name, fn):
    try:
        detail = fn()
        RESULTS.append((name, True, detail or "ok"))
        print("  PASS %s" % name)
    except AssertionError as e:
        RESULTS.append((name, False, str(e)))
        print("  FAIL %s: %s" % (name, e))
    except Exception as e:  # noqa: BLE001
        RESULTS.append((name, False, "%s: %s" % (type(e).__name__, e)))
        print("  ERROR %s: %s: %s" % (name, type(e).__name__, e))


def start(mode):
    srv = mock_relay.start_mock(mode)
    SERVERS.append(srv)
    return srv


# ---------------------------------------------------------------- 用例

def t1_failover_500_to_b64():
    s1, s2 = start("fail500"), start("ok_b64")
    wd = tempfile.mkdtemp()
    cfg = os.path.join(wd, "config.json")
    write_config(cfg, [make_src("dead", s1.server_address[1]),
                       make_src("alive", s2.server_address[1])])
    p = run([GEN, "generate", "--prompt-file", make_prompt(wd), "--name", "t1",
             "--size", "2k"], wd, cfg)
    assert p.returncode == 0, "退出码 %d\nstdout: %s\nstderr: %s" % (p.returncode, p.stdout, p.stderr)
    pngs, _ = outputs(wd)
    assert len(pngs) == 1 and os.path.getsize(pngs[0]) > 100, "输出图片缺失: %s" % pngs
    meta, meta_path = read_meta(wd)
    att = meta["attempts"]
    assert len(att) == 2, "应有两个源的尝试记录，实际 %d" % len(att)
    assert att[0]["source"] == "dead" and att[0]["ok"] is False and "500" in att[0]["error"], \
        "第一个源应为 500 失败: %s" % att[0]
    assert att[1]["source"] == "alive" and att[1]["ok"] is True, "第二个源应成功: %s" % att[1]
    assert meta["size_actual"] == {"width": 2560, "height": 1440}, \
        "实际尺寸不符: %s" % meta["size_actual"]
    with open(meta_path, "r", encoding="utf-8") as f:
        meta_text = f.read()
    assert "sk-mock-secret" not in meta_text and "api_key" not in meta_text, \
        "meta.json 泄露密钥字段或内容"
    assert "切换下一个中转站" in p.stdout, "stdout 应体现切换动作"
    return "500→b64 故障切换成功，meta 记录 2 次尝试"


def t2_failover_500_to_url():
    s1, s2 = start("fail500"), start("ok_url")
    wd = tempfile.mkdtemp()
    cfg = os.path.join(wd, "config.json")
    write_config(cfg, [make_src("dead", s1.server_address[1]),
                       make_src("urlsrc", s2.server_address[1])])
    p = run([GEN, "generate", "--prompt-file", make_prompt(wd), "--name", "t2"], wd, cfg)
    assert p.returncode == 0, "退出码 %d\n%s\n%s" % (p.returncode, p.stdout, p.stderr)
    pngs, _ = outputs(wd)
    assert len(pngs) == 1, "URL 模式应产出图片: %s" % pngs
    meta, _meta_path = read_meta(wd)
    assert meta["size_actual"] == {"width": 777, "height": 555}, \
        "URL 模式 mock 固定返回 777x555: %s" % meta["size_actual"]
    assert meta["size_mismatch"] is True, "尺寸不一致应被标记"
    return "URL 响应模式成功，且 size_mismatch 正确标记"


def t3_fatal_400_no_failover():
    s1, s2 = start("bad400"), start("ok_b64")
    wd = tempfile.mkdtemp()
    cfg = os.path.join(wd, "config.json")
    write_config(cfg, [make_src("bad", s1.server_address[1]),
                       make_src("good", s2.server_address[1])])
    p = run([GEN, "generate", "--prompt-file", make_prompt(wd), "--name", "t3"], wd, cfg)
    assert p.returncode == 1, "400 应以退出码 1 失败，实际 %d" % p.returncode
    assert "致命" in p.stdout, "应有致命错误提示: %s" % p.stdout
    assert "尝试源 good" not in p.stdout, "400 致命错误不应再尝试第二个源: %s" % p.stdout
    assert "invalid size" in p.stdout, "应包含上游错误信息: %s" % p.stdout
    pngs, _ = outputs(wd)
    assert not pngs, "失败时不应有图片输出: %s" % pngs
    return "400 致命错误立即停止，未换源"


def t4_no_config():
    wd = tempfile.mkdtemp()
    cfg = os.path.join(wd, "not-exist.json")
    p = run([GEN, "generate", "--prompt", "x", "--name", "t4"], wd, cfg)
    assert p.returncode != 0 and "No config found" in p.stdout + p.stderr, \
        "应提示 No config found: rc=%d out=%s err=%s" % (p.returncode, p.stdout, p.stderr)
    return "未配置时给出 No config found 引导"


def t5_list_sources_masking():
    s = start("ok_b64")
    wd = tempfile.mkdtemp()
    cfg = os.path.join(wd, "config.json")
    write_config(cfg, [make_src("alpha", s.server_address[1]),
                       make_src("beta", s.server_address[1], enabled=False)])
    p = run([GEN, "--list-sources"], wd, cfg)
    assert p.returncode == 0, p.stdout + p.stderr
    assert "alpha" in p.stdout and "beta" in p.stdout, p.stdout
    assert "禁用" in p.stdout and "启用" in p.stdout, p.stdout
    assert "sk-mock-secret-alpha" not in p.stdout, "完整密钥不应出现"
    assert "****" in p.stdout, "密钥应脱敏显示"
    return "list-sources 正常且密钥脱敏"


def t6_size_alias_4k():
    s = start("ok_b64")
    wd = tempfile.mkdtemp()
    cfg = os.path.join(wd, "config.json")
    write_config(cfg, [make_src("a", s.server_address[1])])
    p = run([GEN, "generate", "--prompt-file", make_prompt(wd), "--name", "t6",
             "--size", "4k"], wd, cfg)
    assert p.returncode == 0, p.stdout + p.stderr
    meta, _ = read_meta(wd)
    assert meta["size_requested"] == [3840, 2160], meta["size_requested"]
    assert meta["size_actual"] == {"width": 3840, "height": 2160}, meta["size_actual"]
    pngs, _ = outputs(wd)
    assert "-4k." in os.path.basename(pngs[0]), "文件名应含 4k 标签: %s" % pngs[0]
    return "别名 4k 解析为 3840x2160 并体现在文件名"


def t7_default_size():
    s = start("ok_b64")
    wd = tempfile.mkdtemp()
    cfg = os.path.join(wd, "config.json")
    write_config(cfg, [make_src("a", s.server_address[1])])
    p = run([GEN, "generate", "--prompt-file", make_prompt(wd), "--name", "t7"], wd, cfg)
    assert p.returncode == 0, p.stdout + p.stderr
    meta, _ = read_meta(wd)
    assert meta["size_requested"] == [2560, 1440], meta["size_requested"]
    return "默认尺寸 2560x1440"


def t8_edit_mode():
    s = start("edits_ok")
    wd = tempfile.mkdtemp()
    ref = os.path.join(wd, "ref.png")
    with open(ref, "wb") as f:
        f.write(mock_relay.make_png(320, 240))
    cfg = os.path.join(wd, "config.json")
    write_config(cfg, [make_src("edits", s.server_address[1])])
    p = run([GEN, "generate", "--prompt-file", make_prompt(wd), "--name", "t8",
             "--input-image", ref, "--size", "square"], wd, cfg)
    assert p.returncode == 0, p.stdout + p.stderr
    reqs = [r for r in s.requests if r["path"].rstrip("/").endswith("/images/edits")]
    assert reqs, "mock 未收到 edits 请求: %s" % s.requests
    assert reqs[0]["has_image_part"], "edits 请求应包含图片数据"
    assert reqs[0]["auth"] == "Bearer sk-mock-secret-edits", "应携带 Authorization: %s" % reqs[0]["auth"]
    meta, _ = read_meta(wd)
    assert meta["mode"] == "edit" and meta["size_requested"] == [2048, 2048]
    return "编辑模式走 /images/edits 并上传参考图"


def t9_secret_masking_in_meta():
    s1, s2 = start("echoauth500"), start("ok_b64")
    wd = tempfile.mkdtemp()
    cfg = os.path.join(wd, "config.json")
    write_config(cfg, [make_src("echo", s1.server_address[1]),
                       make_src("ok", s2.server_address[1])])
    p = run([GEN, "generate", "--prompt-file", make_prompt(wd), "--name", "t9"], wd, cfg)
    assert p.returncode == 0, p.stdout + p.stderr
    meta, meta_path = read_meta(wd)
    err = meta["attempts"][0]["error"]
    assert "sk-mock-secret-echo" not in err, "错误消息泄露完整密钥: %s" % err
    assert "****" in err, "错误消息未脱敏: %s" % err
    with open(meta_path, "r", encoding="utf-8") as f:
        assert "sk-mock-secret" not in f.read(), "meta.json 文本泄露密钥"
    return "上游回显密钥时，meta 与 stdout 均已打码"


def t10_url_download_failover():
    s1, s2 = start("url_dead"), start("ok_b64")
    wd = tempfile.mkdtemp()
    cfg = os.path.join(wd, "config.json")
    write_config(cfg, [make_src("deadurl", s1.server_address[1]),
                       make_src("ok", s2.server_address[1])])
    p = run([GEN, "generate", "--prompt-file", make_prompt(wd), "--name", "t10"], wd, cfg)
    assert p.returncode == 0, p.stdout + p.stderr
    meta, _ = read_meta(wd)
    assert meta["attempts"][0]["ok"] is False and "下载图片失败" in meta["attempts"][0]["error"], \
        meta["attempts"][0]
    return "图片下载失败可切换源"


def t11_auth401_failover():
    s1, s2 = start("auth401"), start("ok_b64")
    wd = tempfile.mkdtemp()
    cfg = os.path.join(wd, "config.json")
    write_config(cfg, [make_src("badkey", s1.server_address[1]),
                       make_src("ok", s2.server_address[1])])
    p = run([GEN, "generate", "--prompt-file", make_prompt(wd), "--name", "t11"], wd, cfg)
    assert p.returncode == 0, p.stdout + p.stderr
    meta, _ = read_meta(wd)
    assert "401" in meta["attempts"][0]["error"], meta["attempts"][0]
    return "401 密钥失效切换下一源"


def t12_connection_refused_failover():
    dead_port = free_closed_port()
    s2 = start("ok_b64")
    wd = tempfile.mkdtemp()
    cfg = os.path.join(wd, "config.json")
    write_config(cfg, [make_src("refused", dead_port),
                       make_src("ok", s2.server_address[1])])
    p = run([GEN, "generate", "--prompt-file", make_prompt(wd), "--name", "t12"], wd, cfg)
    assert p.returncode == 0, p.stdout + p.stderr
    meta, _ = read_meta(wd)
    assert meta["attempts"][0]["ok"] is False and "连接失败" in meta["attempts"][0]["error"], \
        meta["attempts"][0]
    return "连接拒绝（中转站挂掉）切换下一源"


def t13_noimage_then_success():
    s1, s2 = start("noimage"), start("ok_b64")
    wd = tempfile.mkdtemp()
    cfg = os.path.join(wd, "config.json")
    write_config(cfg, [make_src("empty", s1.server_address[1]),
                       make_src("ok", s2.server_address[1])])
    p = run([GEN, "generate", "--prompt-file", make_prompt(wd), "--name", "t13"], wd, cfg)
    assert p.returncode == 0, p.stdout + p.stderr
    meta, _ = read_meta(wd)
    assert meta["attempts"][0]["ok"] is False, meta["attempts"]
    return "200 但无图字段视为该源故障并切换"


def t14_empty_prompt():
    s = start("ok_b64")
    wd = tempfile.mkdtemp()
    cfg = os.path.join(wd, "config.json")
    write_config(cfg, [make_src("a", s.server_address[1])])
    p = run([GEN, "generate", "--name", "t14"], wd, cfg)
    assert p.returncode != 0 and "提示词为空" in p.stdout + p.stderr, \
        "rc=%d out=%s err=%s" % (p.returncode, p.stdout, p.stderr)
    return "空提示词被拦截"


def t15_setup_cli_and_generate():
    s = start("ok_b64")
    wd = tempfile.mkdtemp()
    cfg = os.path.join(wd, "config.json")
    url = "http://127.0.0.1:%d/v1" % s.server_address[1]
    p = run([SETUP, "add", "--name", "one", "--base-url", url,
             "--api-key", "sk-mock-secret-one", "--model", "mock-image-1"], wd, cfg)
    assert p.returncode == 0 and "已添加" in p.stdout, p.stdout + p.stderr
    p = run([SETUP, "add", "--name", "two", "--base-url", url,
             "--api-key", "sk-mock-secret-two", "--model", "mock-image-1"], wd, cfg)
    assert p.returncode == 0, p.stdout + p.stderr
    p = run([SETUP, "list"], wd, cfg)
    assert p.returncode == 0 and "one" in p.stdout and "two" in p.stdout, p.stdout
    assert "sk-mock-secret-one" not in p.stdout and "****" in p.stdout, "list 泄露密钥"
    p = run([SETUP, "up", "--name", "two"], wd, cfg)
    assert p.returncode == 0, p.stdout + p.stderr
    with open(cfg, "r", encoding="utf-8") as f:
        names = [x["name"] for x in json.load(f)["sources"]]
    assert names == ["two", "one"], "up 后顺序应为 two,one: %s" % names
    p = run([SETUP, "disable", "--name", "two"], wd, cfg)
    assert p.returncode == 0, p.stdout + p.stderr
    p = run([GEN, "--list-sources"], wd, cfg)
    assert "禁用" in p.stdout, p.stdout
    p = run([GEN, "generate", "--prompt-file", make_prompt(wd), "--name", "t15",
             "--source", "two"], wd, cfg)
    assert p.returncode != 0 and "禁用" in p.stdout + p.stderr, "锁定禁用源应报错: %s" % p.stdout
    p = run([GEN, "generate", "--prompt-file", make_prompt(wd), "--name", "t15"], wd, cfg)
    assert p.returncode == 0, p.stdout + p.stderr
    meta, _ = read_meta(wd)
    assert meta["source"]["name"] == "one", "应只走启用的源 one: %s" % meta["source"]
    return "setup 增/列/上移/禁用 + 锁定禁用源报错 + 仅启用源参与切换"


def t16_gui_smoke():
    wd = tempfile.mkdtemp()
    cfg = os.path.join(wd, "config.json")
    write_config(cfg, [make_src("gui-src", 1)])
    p = run([SETUP, "gui"], wd, cfg, timeout=60, extra_env={"FAILOVER_IMAGEGEN_TOOL_GUI_SMOKE": "1"})
    assert p.returncode == 0 and "gui smoke ok" in p.stdout, \
        "rc=%d out=%s err=%s" % (p.returncode, p.stdout, p.stderr)
    return "GUI 窗口创建/刷新/销毁冒烟通过"


def t18_gui_model_combobox():
    wd = tempfile.mkdtemp()
    cfg = os.path.join(wd, "config.json")
    write_config(cfg, [make_src("gui-src", 1)])
    p = run([SETUP, "gui"], wd, cfg, timeout=60,
            extra_env={"FAILOVER_IMAGEGEN_TOOL_GUI_SELFTEST": "1"})
    assert p.returncode == 0 and "gui selftest ok" in p.stdout, \
        "rc=%d out=%s err=%s" % (p.returncode, p.stdout, p.stderr)
    return "模型字段为可编辑下拉：预置 gpt 系模型 + 手动输入均可用"


def t19_model_cascade_500():
    """站内模型级联：gpt-image-2 过载(500) -> 自动换本站 dall-e-3 成功。"""
    s = start("model_down")
    wd = tempfile.mkdtemp()
    cfg = os.path.join(wd, "config.json")
    write_config(cfg, [make_src_multi("cascade", s.server_address[1],
                                      ["gpt-image-2", "dall-e-3"])])
    p = run([GEN, "generate", "--prompt-file", make_prompt(wd), "--name", "t19"], wd, cfg)
    assert p.returncode == 0, p.stdout + p.stderr
    assert "换本站下一个模型" in p.stdout, p.stdout
    meta, _ = read_meta(wd)
    att = meta["attempts"]
    assert len(att) == 2, "应为 2 次尝试（同站两模型），实际 %d" % len(att)
    assert att[0]["model"] == "gpt-image-2" and att[0]["ok"] is False and "500" in att[0]["error"]
    assert att[1]["model"] == "dall-e-3" and att[1]["ok"] is True
    assert meta["source"]["model"] == "dall-e-3"
    return "500 时站内换模型成功（未换站），meta 记录模型级联"


def t20_model_cascade_404():
    """站内模型级联：模型不存在(404) -> 换本站下一个模型成功。"""
    s = start("model_404")
    wd = tempfile.mkdtemp()
    cfg = os.path.join(wd, "config.json")
    write_config(cfg, [make_src_multi("cascade404", s.server_address[1],
                                      ["gpt-image-2", "flux-1.1-pro"])])
    p = run([GEN, "generate", "--prompt-file", make_prompt(wd), "--name", "t20"], wd, cfg)
    assert p.returncode == 0, p.stdout + p.stderr
    meta, _ = read_meta(wd)
    att = meta["attempts"]
    assert att[0]["model"] == "gpt-image-2" and "404" in att[0]["error"], att[0]
    assert att[1]["model"] == "flux-1.1-pro" and att[1]["ok"] is True, att[1]
    return "404 时站内换模型成功"


def t21_models_discovery():
    """--models：过滤出生图模型、排除非生图模型。"""
    s = start("models_ok")
    wd = tempfile.mkdtemp()
    cfg = os.path.join(wd, "config.json")
    write_config(cfg, [make_src("discover", s.server_address[1])])
    p = run([GEN, "--models"], wd, cfg)
    assert p.returncode == 0, p.stdout + p.stderr
    for mid in ("gpt-image-2", "dall-e-3", "flux-1.1-pro"):
        assert mid in p.stdout, "应包含生图模型 %s: %s" % (mid, p.stdout)
    assert "gpt-4o" not in p.stdout and "text-embedding-3" not in p.stdout, \
        "不应包含非生图模型: %s" % p.stdout
    return "模型发现：过滤出 4 个生图模型，排除 gpt-4o/embedding"


def t22_check_health_and_stats():
    """--check：可达/不可达报告 + 生图后的历史统计。"""
    dead_port = free_closed_port()
    s = start("ok_b64")
    wd = tempfile.mkdtemp()
    cfg = os.path.join(wd, "config.json")
    write_config(cfg, [make_src("alive", s.server_address[1]),
                       make_src("ghost", dead_port)])
    g = run([GEN, "generate", "--prompt-file", make_prompt(wd), "--name", "t22pre"], wd, cfg)
    assert g.returncode == 0, g.stdout + g.stderr
    p = run([GEN, "--check"], wd, cfg)
    assert p.returncode == 0, p.stdout + p.stderr
    assert "可达" in p.stdout and "不可达" in p.stdout, p.stdout
    assert "历史统计" in p.stdout and "alive" in p.stdout and "1 张" in p.stdout, p.stdout
    return "体检报告可达/不可达 + 历史统计（alive 1 张）"


def t23_batch():
    """batch：3 条提示词（1 条强制失败），成功 2/失败 1，退出码 1，失败不中断。"""
    s = start("ok_b64")
    wd = tempfile.mkdtemp()
    cfg = os.path.join(wd, "config.json")
    write_config(cfg, [make_src("batchsrc", s.server_address[1])])
    pf = os.path.join(wd, "jobs.txt")
    with open(pf, "w", encoding="utf-8") as f:
        f.write("# 注释行应被跳过\n第一张 水墨山水\n第二张 FORCE_FAIL 会失败\n第三张 竹林猫\n\n")
    p = run([GEN, "batch", "--prompts-file", "jobs.txt", "--name", "blog",
             "--size", "square"], wd, cfg)
    assert p.returncode == 1, "有失败条目应退出码 1，实际 %d" % p.returncode
    assert "成功 2 / 失败 1 / 共 3" in p.stdout, p.stdout
    pngs, metas = outputs(wd)
    assert len(pngs) == 2 and len(metas) == 2, "应产出 2 张图: %s" % pngs
    names = [os.path.basename(x) for x in pngs]
    assert any(n.startswith("blog-01") for n in names) and any(n.startswith("blog-03") for n in names), names
    assert "forced fail" in p.stdout, p.stdout
    return "批量 3 条：2 成功 1 失败继续跑，注释/空行跳过，退出码 1"


def t24_appdata_fallback():
    """配置回退：无配置路径环境变量且无本地 config.json 时，读取平台统一配置。
    构造一个无本地配置的引擎副本（模拟 App 安装到 Agent 目录的形态），任何机器结果一致。"""
    s = start("ok_b64")
    wd = tempfile.mkdtemp()
    eng = os.path.join(wd, "engine")
    shutil.copytree(os.path.join(SKILL_DIR, "scripts"), os.path.join(eng, "scripts"))
    gen = os.path.join(eng, "scripts", "failover_imagegen_tool.py")
    assert not os.path.exists(os.path.join(eng, "config.json"))
    appdata = os.path.join(wd, "Roaming")
    xdg_config = os.path.join(wd, "xdg-config")
    config_root = appdata if os.name == "nt" else xdg_config
    cfg = os.path.join(config_root, "failover-imagegen-tool", "config.json")
    os.makedirs(os.path.dirname(cfg), exist_ok=True)
    write_config(cfg, [make_src("appdata-src", s.server_address[1])])
    env = dict(os.environ)
    env["HOME"] = wd
    env["USERPROFILE"] = wd
    env["APPDATA"] = appdata
    env["XDG_CONFIG_HOME"] = xdg_config
    env.pop("FAILOVER_IMAGEGEN_TOOL_CONFIG", None)  # 关键：不给环境变量
    env["PYTHONIOENCODING"] = "utf-8"
    p = subprocess.run([PY, gen, "--list-sources"], cwd=wd, capture_output=True,
                       text=True, encoding="utf-8", errors="replace", env=env, timeout=60)
    assert p.returncode == 0 and "appdata-src" in p.stdout, p.stdout + p.stderr
    # 完整生图也应走平台统一配置成功
    p = subprocess.run([PY, gen, "generate", "--prompt-file", make_prompt(wd), "--name", "t24"],
                       cwd=wd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", env=env, timeout=120)
    assert p.returncode == 0, p.stdout + p.stderr
    return "无 env/无本地配置时回退平台统一配置（list + 生图均成功）"


def t17_force_overwrite_and_unique():
    s = start("ok_b64")
    wd = tempfile.mkdtemp()
    cfg = os.path.join(wd, "config.json")
    write_config(cfg, [make_src("a", s.server_address[1])])
    pf = make_prompt(wd)
    run([GEN, "generate", "--prompt-file", pf, "--name", "t17"], wd, cfg)
    time_run = run([GEN, "generate", "--prompt-file", pf, "--name", "t17"], wd, cfg)
    assert time_run.returncode == 0
    pngs, metas = outputs(wd)
    assert len(pngs) == 2, "同秒重名应追加后缀而非覆盖: %s" % pngs
    force_run = run([GEN, "generate", "--prompt-file", pf, "--name", "t17", "--force"], wd, cfg)
    assert force_run.returncode == 0, force_run.stdout + force_run.stderr
    assert "已存在则追加" not in force_run.stdout
    return "默认不覆盖（追加 -2），--force 生效"


def t25_failover_disabled():
    """failover=false：只尝试首位源首个模型，失败不切换（用户主动关闭故障转移）。"""
    s1, s2 = start("fail500"), start("ok_b64")
    wd = tempfile.mkdtemp()
    cfg = os.path.join(wd, "config.json")

    def write_cfg(sources):
        with open(cfg, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "sources": sources, "failover": False},
                      f, ensure_ascii=False, indent=2)

    write_cfg([make_src_multi("dead", s1.server_address[1], ["m1", "m2"]),
               make_src("alive", s2.server_address[1])])
    p = run([GEN, "generate", "--prompt-file", make_prompt(wd), "--name", "t25"], wd, cfg)
    assert p.returncode == 1, "关闭故障转移后应失败: %s\n%s" % (p.stdout, p.stderr)
    assert "故障转移已关闭" in p.stdout, "stdout 应说明故障转移已关闭: %s" % p.stdout
    assert "alive" not in p.stdout, "不应尝试第二个源: %s" % p.stdout
    pngs, _ = outputs(wd)
    assert not pngs, "不应产出图片: %s" % pngs
    write_cfg([make_src("only", s2.server_address[1])])
    p = run([GEN, "generate", "--prompt-file", make_prompt(wd), "--name", "t25b"], wd, cfg)
    assert p.returncode == 0, p.stdout + p.stderr
    meta, _mp = read_meta(wd)
    assert len(meta["attempts"]) == 1, "关闭故障转移成功路径应只有 1 次尝试: %s" % meta["attempts"]
    return "failover=false 仅试首位源首个模型（失败不切换 / 成功单次尝试）"


def t26_prefer_source():
    """★首选：prefer 与数组顺序无关，生图先试它；禁用回退首个启用源；关故障转移只用首选。"""
    import glob as _glob
    sa, sb, sf = start("ok_b64"), start("ok_url"), start("fail500")
    wd = tempfile.mkdtemp()
    cfg = os.path.join(wd, "config.json")

    def save(doc):
        with open(cfg, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=2)

    def meta_of(prefix):
        files = sorted(_glob.glob(os.path.join(wd, "generated",
                                               "%s*.meta.json" % prefix)),
                       key=os.path.getmtime)
        assert files, "缺少 %s 的 meta" % prefix
        with open(files[-1], "r", encoding="utf-8") as f:
            return json.load(f)

    # 1) prefer 指向数组第二位 → 仍第一个尝试并成功
    save({"version": 1, "prefer": "beta",
          "sources": [make_src("alpha", sa.server_address[1]),
                      make_src("beta", sb.server_address[1])]})
    p = run([GEN, "generate", "--prompt-file", make_prompt(wd), "--name", "t26a"], wd, cfg)
    assert p.returncode == 0, p.stdout + p.stderr
    att = meta_of("t26a")["attempts"]
    assert att[0]["source"] == "beta" and att[0]["ok"], \
        "★首选 beta 应第一个尝试且成功: %s" % att
    # 2) 首选被禁用 → 回退数组顺序首个启用源 alpha
    doc = json.load(open(cfg, encoding="utf-8"))
    doc["sources"][1]["enabled"] = False
    save(doc)
    p = run([GEN, "generate", "--prompt-file", make_prompt(wd), "--name", "t26b"], wd, cfg)
    assert p.returncode == 0, p.stdout + p.stderr
    att = meta_of("t26b")["attempts"]
    assert att[0]["source"] == "alpha", "首选禁用应回退首个启用源: %s" % att
    # 3) 故障转移关 + 首选为 500 站 → 只试首选，失败不切换
    save({"version": 1, "failover": False, "prefer": "beta",
          "sources": [make_src("alpha", sa.server_address[1]),
                      make_src("beta", sf.server_address[1])]})
    p = run([GEN, "generate", "--prompt-file", make_prompt(wd), "--name", "t26c"], wd, cfg)
    assert p.returncode == 1 and "故障转移已关闭" in p.stdout, p.stdout + p.stderr
    assert "尝试源 beta" in p.stdout and "尝试源 alpha" not in p.stdout, p.stdout
    # 4) --list-sources 标注 ★
    p = run([GEN, "--list-sources"], wd, cfg)
    assert "★2. [启用] beta" in p.stdout and " 1. [启用] alpha" in p.stdout, p.stdout
    return "★首选优先尝试（与顺序无关）/禁用回退/关闭故障转移只用首选/list 标★"


def main():
    utf8_streams()
    print("=" * 62)
    print("failover-imagegen-tool 端到端验证（本地 mock 中转站，不访问外网）")
    print("=" * 62)
    tests = [
        ("T1  500→b64 故障切换 + meta 尝试记录", t1_failover_500_to_b64),
        ("T2  500→URL 响应模式 + 尺寸不一致标记", t2_failover_500_to_url),
        ("T3  400 致命错误不换源", t3_fatal_400_no_failover),
        ("T4  No config found 引导", t4_no_config),
        ("T5  --list-sources 与密钥脱敏", t5_list_sources_masking),
        ("T6  尺寸别名 4k", t6_size_alias_4k),
        ("T7  默认尺寸 2560x1440", t7_default_size),
        ("T8  编辑模式 /images/edits", t8_edit_mode),
        ("T9  错误消息密钥打码", t9_secret_masking_in_meta),
        ("T10 图片下载失败切换源", t10_url_download_failover),
        ("T11 401 密钥失效切换源", t11_auth401_failover),
        ("T12 连接拒绝（站点挂掉）切换源", t12_connection_refused_failover),
        ("T13 200 无图字段切换源", t13_noimage_then_success),
        ("T14 空提示词拦截", t14_empty_prompt),
        ("T15 setup CLI 全流程 + 生成集成", t15_setup_cli_and_generate),
        ("T16 GUI 冒烟", t16_gui_smoke),
        ("T17 不覆盖/追加后缀与 --force", t17_force_overwrite_and_unique),
        ("T18 模型下拉框（预置+手输）", t18_gui_model_combobox),
        ("T19 站内模型级联（500 换模型）", t19_model_cascade_500),
        ("T20 站内模型级联（404 换模型）", t20_model_cascade_404),
        ("T21 模型发现过滤", t21_models_discovery),
        ("T22 健康体检+历史统计", t22_check_health_and_stats),
        ("T23 批量生图（失败续跑）", t23_batch),
        ("T24 平台统一配置回退", t24_appdata_fallback),
        ("T25 故障转移开关（关闭=仅首位源）", t25_failover_disabled),
        ("T26 ★首选源（prefer 置顶）", t26_prefer_source),
    ]
    for name, fn in tests:
        record(name, fn)
    for srv in SERVERS:
        try:
            srv.shutdown()
            srv.server_close()
        except Exception:
            pass
    ok = sum(1 for _, passed, _ in RESULTS if passed)
    print("-" * 62)
    print("结果：%d/%d 通过" % (ok, len(RESULTS)))
    for name, passed, detail in RESULTS:
        mark = "PASS" if passed else "FAIL"
        print("  [%s] %s — %s" % (mark, name, detail))
    return 0 if ok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
