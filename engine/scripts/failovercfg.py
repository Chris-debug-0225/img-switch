"""failover-imagegen-tool 共享配置模块：加载/保存 config.json 与中转源管理。

config.json 结构：
{
  "version": 1,
  "sources": [
    {
      "name": "example-source",                 # 唯一名称
      "base_url": "https://relay.example.com/v1",   # OpenAI 兼容根地址，通常以 /v1 结尾
      "api_key": "sk-xxx",             # 只保存在本机，禁止提交/分享
      "model": "gpt-image-2.5-flare",  # 单模型；也可用 "models": ["a","b"] 配置站内级联
      "enabled": true,
      "timeout": 300                   # 可选，秒
    }
  ]
}
sources 数组顺序即故障切换优先级：排在前的先用；
源内 models 数组顺序即站内模型级联顺序（前面的模型失败先换本站下一个模型）。
"""
import json
import os
import re
import urllib.parse

CONFIG_ENV = "FAILOVER_IMAGEGEN_TOOL_CONFIG"
CONFIG_FILENAME = "config.json"
DEFAULT_TIMEOUT = 300  # 生图（尤其 4K）耗时较长，默认放宽到 5 分钟

SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{6,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9_\-.]{6,}", re.IGNORECASE),
]


def skill_root():
    """skill 根目录（本文件位于 scripts/ 下）。"""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def appdata_config_path():
    """App 统一配置路径（Windows AppData / 其他平台 ~/.config）。"""
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.join(
            os.path.expanduser("~"), "AppData", "Roaming")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
            os.path.expanduser("~"), ".config")
    return os.path.join(base, "failover-imagegen-tool", "config.json")


def config_path():
    """配置文件路径优先级：
    1. 环境变量 FAILOVER_IMAGEGEN_TOOL_CONFIG
    2. skill 根目录下已存在的 config.json（手动安装/手动管理时向后兼容）
    3. AppData 统一配置（本机开关 App 管理，多 Agent 共享，不存在时作为写入目标）
    """
    env = os.environ.get(CONFIG_ENV)
    if env:
        return os.path.abspath(env)
    local = os.path.join(skill_root(), CONFIG_FILENAME)
    if os.path.exists(local):
        return local
    return appdata_config_path()


def default_config():
    return {"version": 1, "sources": []}


def load_config(path=None):
    """读取配置；文件不存在返回 None（视为未配置），格式错误抛异常。"""
    path = path or config_path()
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError) as e:
        raise RuntimeError("配置文件解析失败 %s: %s" % (path, e))
    if not isinstance(cfg, dict) or not isinstance(cfg.get("sources"), list):
        raise RuntimeError(
            '配置文件格式不正确 %s：顶层应为 {"version":1,"sources":[...]}' % path
        )
    for i, src in enumerate(cfg["sources"]):
        _check_source(src, "sources[%d]" % i)
    return cfg


def save_config(cfg, path=None):
    """原子写入配置（先写临时文件再替换）。"""
    path = path or config_path()
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def _check_source(src, where):
    if not isinstance(src, dict):
        raise RuntimeError("%s 不是对象" % where)
    for key in ("name", "base_url", "api_key"):
        if not str(src.get(key) or "").strip():
            raise RuntimeError("%s 缺少必填字段 %s" % (where, key))
    if not source_models(src):
        raise RuntimeError("%s 需要至少配置一个模型（model 或 models）" % where)
    if "://" not in src["base_url"]:
        raise RuntimeError("%s.base_url 需要是完整的 http(s) 地址" % where)


def source_models(src):
    """源的模型级联列表：优先读 models 数组，兼容旧的单 model 字段。"""
    models = src.get("models")
    if isinstance(models, list):
        out = [str(m).strip() for m in models if str(m).strip()]
        if out:
            return out
    m = str(src.get("model") or "").strip()
    return [m] if m else []


def models_display(src):
    return " → ".join(source_models(src)) or "（未配置模型）"


def normalize_source(name, base_url, api_key, model, enabled=True, timeout=None):
    """构造/规范化一个中转源。model 可传单个字符串或列表（站内模型级联）。"""
    raw = model if isinstance(model, (list, tuple)) else [model]
    models = [str(m).strip() for m in raw if str(m).strip()]
    src = {
        "name": str(name).strip(),
        "base_url": str(base_url).strip().rstrip("/"),
        "api_key": str(api_key).strip(),
        "enabled": bool(enabled),
    }
    if len(models) == 1:
        src["model"] = models[0]
    elif len(models) > 1:
        src["models"] = models
    if timeout:
        src["timeout"] = int(timeout)
    _check_source(src, "source '%s'" % src["name"])
    return src


def find_source(cfg, name):
    for src in cfg["sources"]:
        if src["name"] == name:
            return src
    return None


def enabled_sources(cfg):
    """按优先级（数组顺序）返回已启用的源。"""
    return [s for s in cfg["sources"] if s.get("enabled", True)]


def failover_enabled(cfg):
    """故障转移开关：config.json 顶层 "failover": false 时关闭（默认开启）。
    关闭后生图只尝试首位已启用源的首个模型，失败不切换其他源/模型。"""
    if not isinstance(cfg, dict):
        return True
    return bool(cfg.get("failover", True))


def ordered_sources(cfg):
    """已启用源按生图尝试顺序返回：★首选（顶层 "prefer"=源名，须存在且启用）置顶，
    其余保持数组顺序（后备顺序）。无有效 prefer 时即数组顺序。"""
    srcs = enabled_sources(cfg)
    p = cfg.get("prefer")
    if p:
        first = [s for s in srcs if s.get("name") == p]
        if first:
            return first + [s for s in srcs if s.get("name") != p]
    return srcs


def prefer_index(cfg):
    """★首选源在 cfg["sources"] 里的下标：显式 prefer 且启用优先，否则第一个启用源；
    无启用源返回 None。"""
    srcs = cfg.get("sources") or []
    p = cfg.get("prefer")
    for i, s in enumerate(srcs):
        if p and s.get("name") == p and s.get("enabled", True):
            return i
    for i, s in enumerate(srcs):
        if s.get("enabled", True):
            return i
    return None


def mask_key(key):
    """脱敏展示密钥：sk-abc1234567 -> sk-ab****67。"""
    key = str(key)
    if len(key) <= 8:
        return "*" * len(key)
    return key[:5] + "****" + key[-2:]


def mask_secrets(text):
    """把文本里疑似密钥的部分打码，用于日志/错误消息脱敏。"""
    out = str(text)
    for pat in SECRET_PATTERNS:
        out = pat.sub(lambda m: m.group(0)[:5] + "****", out)
    return out


def endpoint_host(base_url):
    """只展示 URL 主机及端口，不包含用户信息、路径或查询参数。"""
    try:
        parsed = urllib.parse.urlsplit(base_url or "")
        if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
            return "<无效地址>"
        host, port = parsed.hostname, parsed.port
        if ":" in host:
            host = "[%s]" % host
        elif not re.fullmatch(r"[A-Za-z0-9._-]+", host.encode("idna").decode("ascii")):
            return "<无效地址>"
        return "%s:%d" % (host, port) if port is not None else host
    except (ValueError, UnicodeError, TypeError):
        return "<无效地址>"
