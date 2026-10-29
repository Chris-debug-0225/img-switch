#!/usr/bin/env python3
"""failover-imagegen-tool 主脚本：OpenAI 兼容中转站生图，多源×多模型级联故障切换。

用法示例：
  python failover_imagegen_tool.py generate --prompt-file p.txt --name test --size 4k
  python failover_imagegen_tool.py generate --prompt "..." --input-image ref.png   # 编辑模式
  python failover_imagegen_tool.py batch --prompts-file jobs.txt --name blog       # 批量生图
  python failover_imagegen_tool.py --models        # 发现各中转站支持的生图模型
  python failover_imagegen_tool.py --check         # 源健康体检 + 历史成功/耗时统计
  python failover_imagegen_tool.py --list-sources

故障切换规则（中转站优先级为外层，站内模型级联为内层）：
  - 换本站下一个模型：HTTP 404/5xx、响应无图、返回内容不是图片
  - 换下一个中转站：网络错误/超时、401/403/408/429、图片下载失败
  - 致命（立即停止，不再尝试）：HTTP 400/422 等其他 4xx——请求本身不合法
    （模型 ID/尺寸/提示词问题），换站换模型大概率同样失败，不要靠重试绕过

输出：当前目录 generated/ 下 {name}-YYYYMMDD-HHMMSS-{尺寸标签}.png（或 .jpg）
      与同名 .meta.json（模型、尺寸、提示词快照、耗时、逐次尝试记录，分享前需检查敏感内容）。
"""
import argparse
import base64
import glob
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import failovercfg

# 尺寸别名（宽, 高）
SIZE_ALIASES = {
    "2k": (2560, 1440),
    "2k-landscape": (2560, 1440),
    "4k": (3840, 2160),
    "4k-landscape": (3840, 2160),
    "2k-portrait": (1440, 2560),
    "4k-portrait": (2160, 3840),
    "square": (2048, 2048),
    "2k-square": (2048, 2048),
}
DEFAULT_SIZE = (2560, 1440)

# 这些 HTTP 状态视为「站点级故障/限制」：直接换下一个中转站
FAILOVER_SOURCE_HTTP = {401, 403, 408, 429}
# 模型发现时的生图模型关键字（大小写不敏感）
IMAGE_HINT = re.compile(
    r"image|dall|flux|midjourney|\bmj\b|stable|sdxl|sd3|seedream|ideogram|recraft"
    r"|kolors|hidream|pixart|imagen|wanx|imagine|banana|leo", re.IGNORECASE)

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
JPEG_MAGIC = b"\xff\xd8"


class AttemptError(Exception):
    """一次「站×模型」尝试失败。kind: fatal / next_model / next_source。"""

    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind
        self.message = message


# ---------------------------------------------------------------- 尺寸与图片

def parse_size(text):
    """解析尺寸参数：别名或 WxH。返回 (w, h)。"""
    s = (text or "").strip().lower()
    if not s:
        return DEFAULT_SIZE
    if s in SIZE_ALIASES:
        return SIZE_ALIASES[s]
    m = re.fullmatch(r"(\d{2,5})\s*[x×*]\s*(\d{2,5})", s)
    if not m:
        raise ValueError(
            "无法识别的尺寸 %r；可用别名：%s，或直接写 3840x2160 这类 宽x高"
            % (text, "/".join(sorted(SIZE_ALIASES)))
        )
    w, h = int(m.group(1)), int(m.group(2))
    if w < 64 or h < 64:
        raise ValueError("尺寸过小（宽高均需 >= 64）：%dx%d" % (w, h))
    return (w, h)


def size_tag(w, h):
    """输出文件名里的尺寸标签。"""
    m = max(w, h)
    if m >= 3000:
        return "4k"
    if m >= 1500:
        return "2k"
    return "%dx%d" % (w, h)


def detect_image(data):
    """校验字节流是 PNG/JPEG，返回 (ext, w, h)；不是图片返回 None。"""
    if data[:8] == PNG_MAGIC and len(data) >= 24 and data[12:16] == b"IHDR":
        w = int.from_bytes(data[16:20], "big")
        h = int.from_bytes(data[20:24], "big")
        return ("png", w, h)
    if data[:2] == JPEG_MAGIC:
        size = _jpeg_size(data)
        if size:
            return ("jpg", size[0], size[1])
    return None


def _jpeg_size(data):
    i = 2
    n = len(data)
    while i + 9 < n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xD8, 0xD9, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        if marker == 0xFF:  # 填充字节
            i += 1
            continue
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                      0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            h = int.from_bytes(data[i + 5:i + 7], "big")
            w = int.from_bytes(data[i + 7:i + 9], "big")
            return (w, h)
        seg_len = int.from_bytes(data[i + 2:i + 4], "big")
        if seg_len < 2:
            return None
        i += 2 + seg_len
    return None


# ---------------------------------------------------------------- HTTP

def _http_json(req, timeout):
    """发请求并解析 JSON 响应。HTTPError 转成带上游消息的错误。"""
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, _parse_json(raw), raw
    except urllib.error.HTTPError as e:
        raw = b""
        try:
            raw = e.read(2000)
        except Exception:
            pass
        raise _http_error(e.code, raw)


def _http_error(code, raw):
    detail = ""
    parsed = _parse_json(raw)
    if isinstance(parsed, dict):
        err = parsed.get("error") or parsed
        if isinstance(err, dict):
            detail = str(err.get("message") or err.get("code") or "")
        elif err:
            detail = str(err)
    if not detail:
        detail = raw[:200].decode("utf-8", "replace").strip()
    return urllib.error.HTTPError(
        "response", code,
        "HTTP %d: %s" % (code, failovercfg.mask_secrets(detail)), None, None)


def _parse_json(raw):
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return None


def multipart_encode(fields, file_field, file_name, file_bytes, mime):
    boundary = "----failoverimagegentool" + uuid.uuid4().hex
    parts = []
    for k, v in fields.items():
        parts.append(
            ("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
             % (boundary, k, v)).encode("utf-8")
        )
    parts.append(
        ("--%s\r\nContent-Disposition: form-data; name=\"%s\"; filename=\"%s\"\r\n"
         "Content-Type: %s\r\n\r\n" % (boundary, file_field, file_name, mime)).encode("utf-8")
    )
    parts.append(file_bytes)
    parts.append(("\r\n--%s--\r\n" % boundary).encode("utf-8"))
    return b"".join(parts), "multipart/form-data; boundary=%s" % boundary


# ---------------------------------------------------------------- 单次尝试

def attempt_source(src, prompt, size, input_image, model, verbose=False):
    """对单个「站×模型」发起一次生图请求。

    成功返回 dict（含 image_bytes/ext/尺寸/耗时/命中的源与模型）；
    失败抛 AttemptError(kind, message)。
    """
    base = src["base_url"].rstrip("/")
    model = (model or "").strip()
    if not model:
        raise AttemptError("fatal", "源 %r 未配置模型" % src["name"])
    timeout = int(src.get("timeout") or failovercfg.DEFAULT_TIMEOUT)
    editing = bool(input_image)
    endpoint = base + ("/images/edits" if editing else "/images/generations")
    headers = {"Authorization": "Bearer " + src["api_key"]}
    t0 = time.time()

    try:
        if editing:
            with open(input_image, "rb") as f:
                img_bytes = f.read()
            fields = {"model": model, "prompt": prompt, "size": "%dx%d" % size, "n": "1"}
            body, ctype = multipart_encode(
                fields, "image", os.path.basename(input_image), img_bytes,
                "image/png" if img_bytes[:8] == PNG_MAGIC else "image/jpeg",
            )
            headers["Content-Type"] = ctype
            req = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")
        else:
            payload = {"model": model, "prompt": prompt, "size": "%dx%d" % size, "n": 1}
            headers["Content-Type"] = "application/json"
            req = urllib.request.Request(
                endpoint, data=json.dumps(payload).encode("utf-8"),
                headers=headers, method="POST",
            )
        if verbose:
            print("    POST %s model=%s size=%dx%d mode=%s"
                  % (failovercfg.endpoint_host(base), model, size[0], size[1], "edit" if editing else "gen"))
        status, data, _raw = _http_json(req, timeout)
    except urllib.error.HTTPError as e:
        code = e.code
        msg = str(e.reason)  # _http_error 构造时已带 "HTTP <code>: <detail>"
        if code in FAILOVER_SOURCE_HTTP:
            raise AttemptError("next_source", msg)
        if code == 404 or code >= 500:
            raise AttemptError("next_model", msg)
        raise AttemptError("fatal", msg + "（请求被拒绝，检查模型 ID/尺寸/提示词，勿反复重试）")
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
        raise AttemptError("next_source", "连接失败: %s" % failovercfg.mask_secrets(e))

    image_bytes = _extract_image(data, timeout)
    info = detect_image(image_bytes)
    if not info:
        raise AttemptError("next_source", "响应内容不是有效的 PNG/JPEG 图片")
    ext, w, h = info
    return {
        "image_bytes": image_bytes,
        "ext": ext,
        "width": w,
        "height": h,
        "model_returned": (data or {}).get("model") if isinstance(data, dict) else None,
        "endpoint": endpoint,
        "elapsed": round(time.time() - t0, 2),
        "source_name": src["name"],
        "source_host": failovercfg.endpoint_host(src["base_url"]),
        "model": model,
    }


def _extract_image(data, timeout):
    """从 OpenAI 兼容响应中取出图片字节：兼容 b64_json / url / images[]（URL 或 data:URI）。"""
    if not isinstance(data, dict):
        raise AttemptError("next_model", "响应不是 JSON 对象")
    items = data.get("data") if isinstance(data.get("data"), list) else None
    if not items and isinstance(data.get("images"), list):
        items = [{"x": v} for v in data["images"]]
    if not items:
        raise AttemptError("next_model", "响应中没有 data/images 图片字段: %s"
                           % failovercfg.mask_secrets(json.dumps(data, ensure_ascii=False)[:200]))
    item = items[0]
    if isinstance(item, dict) and item.get("b64_json"):
        return base64.b64decode(item["b64_json"])
    url = None
    if isinstance(item, dict):
        url = item.get("url") or item.get("image_url")
    elif isinstance(item, str):
        url = item
    if isinstance(url, dict):
        url = url.get("url")
    if not url:
        raise AttemptError("next_model", "响应条目里没有 url/b64_json")
    if url.startswith("data:"):
        b64 = url.split(",", 1)[1] if "," in url else ""
        return base64.b64decode(b64)
    return _download(url, timeout)


def _download(url, timeout):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "failover-imagegen-tool/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
        raise AttemptError("next_source", "下载图片失败 %s（%s）"
                           % (failovercfg.endpoint_host(url), type(e).__name__)) from None


# ---------------------------------------------------------------- 级联与保存

def _attempt_plan_count(sources, model_override):
    return sum(len([model_override] if model_override else failovercfg.source_models(s))
               for s in sources)


def _run_attempts(sources, prompt, size, input_image, model_override,
                  verbose=False, failover=True):
    """跑完整「站×模型」级联。返回 (result|None, attempts)。
    failover=False 时只尝试首位源的首个模型，失败不切换（用户主动关闭故障转移）。"""
    if not failover:
        sources = sources[:1]
    total = _attempt_plan_count(sources, model_override) if failover else 1
    attempts = []
    idx = 0
    for si, src in enumerate(sources):
        models = [model_override] if model_override else failovercfg.source_models(src)
        if not failover:
            models = models[:1]
        for model in models:
            idx += 1
            host = failovercfg.endpoint_host(src["base_url"])
            print("[%d/%d] 尝试源 %s（%s，模型 %s）..."
                  % (idx, total, src["name"], host, model))
            try:
                result = attempt_source(src, prompt, size, input_image, model,
                                        verbose=verbose)
            except AttemptError as e:
                attempts.append({
                    "source": src["name"], "host": host, "model": model,
                    "ok": False, "strategy": e.kind, "error": e.message,
                })
                if e.kind == "fatal":
                    print("    ✗ 致命错误，停止尝试（不会换站/换模型重试）：%s" % e.message)
                    print("      提示：先确认模型 ID 和允许的尺寸，只调整对应参数；不要为绕过 4xx 反复重试。")
                    return None, attempts
                if not failover:
                    print("    ✗ %s —— 故障转移已关闭，不切换其他源/模型" % e.message)
                    return None, attempts
                if e.kind == "next_source":
                    print("    ✗ %s —— 切换下一个中转站" % e.message)
                    break  # 跳过本站剩余模型，进入下一个源
                if model == models[-1]:  # 本站模型用尽
                    if si < len(sources) - 1:
                        print("    ✗ %s —— 本站模型用尽，切换下一个中转站" % e.message)
                    else:
                        print("    ✗ %s" % e.message)
                else:
                    print("    ✗ %s —— 换本站下一个模型" % e.message)
                continue
            attempts.append({
                "source": src["name"], "host": host, "model": model,
                "ok": True, "elapsed_seconds": result["elapsed"],
            })
            return result, attempts
    return None, attempts


def unique_path(path):
    """已存在则追加 -2/-3 后缀，避免覆盖。"""
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    for i in range(2, 1000):
        cand = "%s-%d%s" % (stem, i, ext)
        if not os.path.exists(cand):
            return cand
    raise RuntimeError("无法生成唯一文件名: " + path)


def save_outputs(outdir, name, force, size, result, prompt, prompt_file,
                 input_image, attempts, started_at):
    """落盘图片 + meta.json，返回 (img_path, meta_path, meta)。"""
    os.makedirs(outdir, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    base_name = "%s-%s-%s" % (name, ts, size_tag(*size))
    img_path = os.path.join(outdir, base_name + "." + result["ext"])
    if not force:
        img_path = unique_path(img_path)  # 已存在则追加 -2/-3，--force 才覆盖
    with open(img_path, "wb") as f:
        f.write(result["image_bytes"])

    meta = {
        "name": name,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
        "source": {
            "name": result["source_name"],
            "host": result["source_host"],
            "model": result["model"],
        },
        "model_requested": result["model"],
        "model_returned": result.get("model_returned"),
        "mode": "edit" if input_image else "generate",
        "size_requested": list(size),
        "size_actual": {"width": result["width"], "height": result["height"]},
        "size_mismatch": [result["width"], result["height"]] != list(size),
        "prompt": prompt,
        "prompt_file": os.path.abspath(prompt_file) if prompt_file else None,
        "input_image": os.path.abspath(input_image) if input_image else None,
        "output_file": os.path.abspath(img_path),
        "output_bytes": len(result["image_bytes"]),
        "duration_seconds": round(time.time() - started_at, 2),
        "attempts": attempts,  # 错误文本会尝试脱敏，仍需在分享前检查
    }
    meta_path = os.path.splitext(img_path)[0] + ".meta.json"
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
        f.write("\n")
    return img_path, meta_path, meta


def _print_success(result, img_path, meta_path, meta, attempts):
    print("    ✓ 成功（%.1fs）" % result["elapsed"])
    if meta["size_mismatch"]:
        print("      注意：实际尺寸 %dx%d 与请求 %dx%d 不一致，以输出为准（详见 meta.json）。"
              % (result["width"], result["height"],
                 meta["size_requested"][0], meta["size_requested"][1]))
    print("图片: %s（%dx%d, %d 字节）" % (img_path, result["width"], result["height"],
                                         len(result["image_bytes"])))
    print("记录: %s" % meta_path)
    if len(attempts) > 1:
        fail_names = "、".join("%s/%s" % (a["source"], a.get("model", "?"))
                               for a in attempts if not a["ok"])
        print("本次共尝试 %d 次（%s 失败后切换），最终由 %s/%s 产出。"
              % (len(attempts), fail_names, result["source_name"], result["model"]))


def _fail_summary(attempts):
    print("\n全部 %d 次尝试（站×模型）失败：" % len(attempts))
    _print_attempts(attempts)
    print("排查建议：确认中转站网络与额度；429/超时多为限流或服务过载；"
          "若超时发生在服务端已接受请求之后，切换可能造成重复生成/计费，先检查输出目录与中转站记录。")


def _print_attempts(attempts):
    for a in attempts:
        if a["ok"]:
            print("  - %s/%s: 成功（%.1fs）" % (a["source"], a.get("model", "?"),
                                                a.get("elapsed_seconds", 0)))
        else:
            print("  - %s/%s: 失败[%s] %s" % (a["source"], a.get("model", "?"),
                                               a.get("strategy", "?"), a["error"]))


# ---------------------------------------------------------------- 命令公共

def read_prompt(args):
    if getattr(args, "prompt_file", None):
        with open(args.prompt_file, "r", encoding="utf-8-sig") as f:
            p = f.read().strip()
    else:
        p = (getattr(args, "prompt", None) or "").strip()
    if not p:
        raise SystemExit("错误：提示词为空。用 --prompt 或 --prompt-file 提供（推荐后者，避免提示词进入终端历史）。")
    if len(p) > 32000:
        raise SystemExit("错误：提示词过长（%d 字符，上限 32000）。" % len(p))
    return p


def _load_cfg_or_exit():
    try:
        cfg = failovercfg.load_config()
    except RuntimeError as e:
        raise SystemExit("错误：%s" % e)
    if cfg is None:
        raise SystemExit(
            "错误：No config found（%s）。先运行 python scripts/setup.py add --name <名> "
            "--base-url <地址> --api-key <密钥> --model <模型> 保存至少一个已启用的中转源，"
            "再用 --list-sources 检查。" % failovercfg.config_path()
        )
    return cfg


def pick_sources(cfg, only_name, model_override=None):
    if only_name:
        src = failovercfg.find_source(cfg, only_name)
        if not src:
            names = ", ".join(s["name"] for s in cfg["sources"]) or "（无）"
            raise SystemExit("错误：配置中不存在源 %r。可用源：%s" % (only_name, names))
        if not src.get("enabled", True):
            raise SystemExit("错误：源 %r 当前处于禁用状态，先用 setup.py enable 启用。" % only_name)
        sources = [src]
    else:
        sources = failovercfg.ordered_sources(cfg)  # ★首选置顶，其余按数组顺序
        if not sources:
            raise SystemExit(
                "错误：没有已启用的中转源。运行 python scripts/setup.py add ...（或 setup.py gui）配置，"
                "再跑 --list-sources 检查。"
            )
    if not model_override:
        for s in sources:
            if not failovercfg.source_models(s):
                raise SystemExit("错误：源 %r 未配置模型，用 setup.py 重新添加并填模型。" % s["name"])
    return sources


def _prepare_generate(args):
    prompt = read_prompt(args)
    try:
        size = parse_size(getattr(args, "size", None))
    except ValueError as e:
        raise SystemExit("错误：%s" % e)
    input_image = getattr(args, "input_image", None)
    if input_image and not os.path.isfile(input_image):
        raise SystemExit("错误：输入图片不存在：%s" % input_image)
    cfg = _load_cfg_or_exit()
    sources = pick_sources(cfg, getattr(args, "source", None),
                           getattr(args, "model", None))
    return prompt, size, input_image, sources, failovercfg.failover_enabled(cfg)


# ---------------------------------------------------------------- generate

def cmd_generate(args):
    prompt, size, input_image, sources, failover = _prepare_generate(args)
    outdir = args.outdir or "generated"
    started_at = time.time()
    chain = " -> ".join(s["name"] for s in (sources[:1] if not failover else sources))
    print("生图：size=%dx%d 模式=%s 源顺序=%s%s"
          % (size[0], size[1], "edit" if input_image else "generate", chain,
             "" if failover else "（故障转移已关闭：仅尝试首位源首个模型）"))
    result, attempts = _run_attempts(sources, prompt, size, input_image,
                                     getattr(args, "model", None), args.verbose,
                                     failover=failover)
    if not result:
        _fail_summary(attempts)
        return 1
    img_path, meta_path, meta = save_outputs(
        outdir, args.name, args.force, size, result, prompt,
        getattr(args, "prompt_file", None), input_image, attempts, started_at)
    _print_success(result, img_path, meta_path, meta, attempts)
    return 0


# ---------------------------------------------------------------- batch

def cmd_batch(args):
    path = args.prompts_file
    if not os.path.isfile(path):
        raise SystemExit("错误：提示词列表文件不存在：%s" % path)
    with open(path, "r", encoding="utf-8-sig") as f:
        lines = [ln.strip() for ln in f.read().splitlines()]
    prompts = [ln for ln in lines if ln and not ln.startswith("#")]
    if not prompts:
        raise SystemExit("错误：%s 里没有有效提示词（一行一图，# 开头为注释）。" % path)

    try:
        size = parse_size(args.size)
    except ValueError as e:
        raise SystemExit("错误：%s" % e)
    cfg = _load_cfg_or_exit()
    sources = pick_sources(cfg, args.source, args.model)
    failover = failovercfg.failover_enabled(cfg)
    outdir = args.outdir or "generated"

    total = len(prompts)
    chain = " -> ".join(s["name"] for s in (sources[:1] if not failover else sources))
    print("批量生图：共 %d 张，size=%dx%d，输出目录 %s，源顺序=%s%s"
          % (total, size[0], size[1], outdir, chain,
             "" if failover else "（故障转移已关闭：仅尝试首位源首个模型）"))
    results = []  # (序号, 提示词, ok, 产物路径, 失败原因)
    t_all = time.time()
    try:
        for i, p in enumerate(prompts, 1):
            print("\n===== [%d/%d] %s =====" % (i, total, p[:40]))
            t0 = time.time()
            result, attempts = _run_attempts(sources, p, size, None,
                                             args.model, args.verbose,
                                             failover=failover)
            if result:
                img_path, _mp, _meta = save_outputs(
                    outdir, "%s-%02d" % (args.name, i), args.force, size, result,
                    p, path, None, attempts, t0)
                print("图片: %s" % img_path)
                results.append((i, p, True, img_path, None))
            else:
                last = attempts[-1] if attempts else {}
                results.append((i, p, False, None,
                                "%s/%s: %s" % (last.get("source", "?"),
                                               last.get("model", "?"),
                                               last.get("error", "未知错误"))))
    except KeyboardInterrupt:
        print("\n已中断（Ctrl+C）。已完成 %d/%d 张。" % (len(results), total))

    ok_n = sum(1 for r in results if r[2])
    fail_n = len(results) - ok_n
    print("\n===== 批量汇总：成功 %d / 失败 %d / 共 %d（总耗时 %.1fs）====="
          % (ok_n, fail_n, total, time.time() - t_all))
    for i, p, ok, out, err in results:
        head = p[:24] + ("…" if len(p) > 24 else "")
        if ok:
            print("  [%d] ✓ %s -> %s" % (i, head, os.path.basename(out)))
        else:
            print("  [%d] ✗ %s -> %s" % (i, head, err))
    if fail_n:
        print("提示：失败的条目可从上面的错误判断原因；致命 400/422 请检查模型 ID 与尺寸后重跑。")
    return 0 if fail_n == 0 and len(results) == total else 1


# ---------------------------------------------------------------- 模型发现

def cmd_models(args):
    cfg = _load_cfg_or_exit()
    sources = pick_sources(cfg, getattr(args, "source", None))
    any_ok = False
    for src in sources:
        base = src["base_url"].rstrip("/")
        host = failovercfg.endpoint_host(base)
        print("源 %s（%s）：" % (src["name"], host))
        req = urllib.request.Request(
            base + "/models", headers={"Authorization": "Bearer " + src["api_key"]})
        timeout = min(20, int(src.get("timeout") or 20))
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = _parse_json(resp.read()) or {}
        except urllib.error.HTTPError as e:
            print("  ✗ HTTP %d：%s（检查密钥/地址）" % (e.code, e.reason))
            continue
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            print("  ✗ 不可达：%s" % failovercfg.mask_secrets(e))
            continue
        ids = []
        items = data.get("data") if isinstance(data, dict) else None
        if isinstance(items, list):
            for it in items:
                mid = it.get("id") if isinstance(it, dict) else it
                if mid:
                    ids.append(str(mid))
        if not ids:
            print("  △ 接口正常但未返回模型列表（该站可能不开放 /models，以服务商文档为准）")
            any_ok = True
            continue
        hits = [i for i in ids if IMAGE_HINT.search(i)]
        if hits:
            print("  生图相关模型（%d 个）：" % len(hits))
            for mid in hits:
                print("    - %s" % mid)
        else:
            print("  未匹配到常见生图模型，该站全部模型（前 15 个）：")
            for mid in ids[:15]:
                print("    - %s" % mid)
        any_ok = True
    if not any_ok:
        print("\n所有源都不可用，先跑 --check 体检。")
        return 1
    print("\n提示：把上面任一模型 ID 填进配置（setup.py add --model，或 GUI 里逗号分隔多个）即可使用。")
    return 0


# ---------------------------------------------------------------- 健康体检

def cmd_check(_args):
    cfg = _load_cfg_or_exit()
    sources = failovercfg.enabled_sources(cfg)
    if not sources:
        raise SystemExit("错误：没有已启用的中转源。")

    print("== 源健康体检 ==")
    ok_count = 0
    for src in sources:
        base = src["base_url"].rstrip("/")
        host = failovercfg.endpoint_host(base)
        req = urllib.request.Request(
            base + "/models", headers={"Authorization": "Bearer " + src["api_key"]})
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=12) as resp:
                resp.read()
            latency = int((time.time() - t0) * 1000)
            print("  ✓ %s（%s）可达，%dms，/models 接口正常" % (src["name"], host, latency))
            ok_count += 1
        except urllib.error.HTTPError as e:
            latency = int((time.time() - t0) * 1000)
            if e.code in (401, 403):
                print("  △ %s（%s）可达但密钥无效（HTTP %d），生图会失败"
                      % (src["name"], host, e.code))
            else:
                print("  △ %s（%s）可达（%dms，HTTP %d 无 /models 接口，生图仍可能可用）"
                      % (src["name"], host, latency, e.code))
                ok_count += 1
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            print("  ✗ %s（%s）不可达：%s" % (src["name"], host, failovercfg.mask_secrets(e)))

    print("\n== 历史统计（%s）==" % os.path.join("generated", "*.meta.json"))
    stats = {}
    for mp in glob.glob(os.path.join("generated", "*.meta.json")):
        try:
            with open(mp, "r", encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, ValueError):
            continue
        name = (meta.get("source") or {}).get("name", "?")
        st = stats.setdefault(name, {"count": 0, "total_sec": 0.0, "switched": 0})
        st["count"] += 1
        st["total_sec"] += float(meta.get("duration_seconds") or 0)
        if len(meta.get("attempts") or []) > 1:
            st["switched"] += 1
    if not stats:
        print("  暂无记录（在当前目录生图后自动积累，可据此用 setup.py up/down 调整源优先级）。")
    else:
        for name, st in sorted(stats.items(), key=lambda kv: -kv[1]["count"]):
            avg = st["total_sec"] / st["count"] if st["count"] else 0
            extra = ("，%d 张靠切换源/模型救回" % st["switched"]) if st["switched"] else ""
            print("  %s：%d 张，平均 %.1fs%s" % (name, st["count"], avg, extra))
    return 0 if ok_count else 1


# ---------------------------------------------------------------- list / main

def cmd_list_sources(_args):
    cfg = _load_cfg_or_exit()
    sources = cfg["sources"]
    if not sources:
        print("配置为空（%s）。用 setup.py add 添加中转源。" % failovercfg.config_path())
        return 1
    print("配置文件: %s（★=生图首选，先于列表顺序尝试；其余按列表顺序后备；"
          "站内模型按顺序级联；仅启用状态的源参与）" % failovercfg.config_path())
    pi = failovercfg.prefer_index(cfg)
    for i, s in enumerate(sources, 1):
        state = "启用" if s.get("enabled", True) else "禁用"
        timeout = s.get("timeout") or failovercfg.DEFAULT_TIMEOUT
        star = "★" if (pi is not None and i - 1 == pi) else " "
        print("%s%d. [%s] %-12s %s  %s  timeout=%ds key=%s"
              % (star, i, state, s["name"], failovercfg.models_display(s), failovercfg.endpoint_host(s["base_url"]),
                 timeout, failovercfg.mask_key(s["api_key"])))
    return 0


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    parser = argparse.ArgumentParser(
        prog="failover_imagegen_tool.py",
        description="OpenAI 兼容中转站生图（多源×多模型级联故障切换，输出到 generated/）",
    )
    parser.add_argument("--list-sources", action="store_true", help="列出已配置的中转源")
    parser.add_argument("--models", action="store_true",
                        help="发现各中转站支持的生图模型（查 /v1/models 并过滤）")
    parser.add_argument("--check", action="store_true",
                        help="源健康体检 + 历史成功/耗时统计")
    sub = parser.add_subparsers(dest="command")

    g = sub.add_parser("generate", help="生成/编辑单张图片")
    g.add_argument("--prompt", help="提示词（推荐改用 --prompt-file，避免进入终端历史）")
    g.add_argument("--prompt-file", help="提示词文件路径（整文件为一条提示词）")
    g.add_argument("--name", default="image", help="输出文件基础名（默认 image）")
    g.add_argument("--source", help="指定使用某个中转源（默认按配置顺序自动故障切换）")
    g.add_argument("--model", help="临时覆盖模型（默认用各源配置的模型级联）")
    g.add_argument("--size", help="尺寸：别名 2k/4k/2k-portrait/4k-portrait/square，或 3840x2160（默认 2560x1440）")
    g.add_argument("--input-image", help="参考图路径，提供后走 /images/edits 编辑模式")
    g.add_argument("--outdir", help="输出目录（默认 ./generated）")
    g.add_argument("--force", action="store_true", help="同名时直接覆盖（默认追加 -2 后缀）")
    g.add_argument("--verbose", action="store_true", help="打印请求细节（不含密钥）")
    g.set_defaults(func=cmd_generate)

    b = sub.add_parser("batch", help="批量生图（一行一图的提示词文件）")
    b.add_argument("--prompts-file", required=True, help="提示词列表文件：一行一条，# 开头为注释")
    b.add_argument("--name", default="batch", help="输出文件基础名（默认 batch，实际为 batch-01/02…）")
    b.add_argument("--source", help="指定使用某个中转源")
    b.add_argument("--model", help="临时覆盖模型")
    b.add_argument("--size", help="尺寸（同 generate）")
    b.add_argument("--outdir", help="输出目录（默认 ./generated）")
    b.add_argument("--force", action="store_true", help="同名时直接覆盖")
    b.add_argument("--verbose", action="store_true", help="打印请求细节（不含密钥）")
    b.set_defaults(func=cmd_batch)

    m = sub.add_parser("models", help="发现各中转站支持的生图模型")
    m.add_argument("--source", help="只查某个源")
    m.set_defaults(func=cmd_models)

    c = sub.add_parser("check", help="源健康体检 + 历史统计")
    c.set_defaults(func=cmd_check)

    args = parser.parse_args(argv)
    if args.list_sources:
        return cmd_list_sources(args)
    if args.models:
        return cmd_models(args)
    if args.check:
        return cmd_check(args)
    if getattr(args, "func", None):
        return args.func(args)
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
