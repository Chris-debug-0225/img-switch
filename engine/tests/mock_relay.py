"""本地模拟中转站：仅监听 127.0.0.1，供 tests/run_tests.py 做端到端验证，不访问外网。

模式（server.mode）：
  fail500     生成/编辑一律返回 500
  echoauth500 返回 500 且错误消息回显完整 Authorization 头（验证密钥脱敏）
  bad400      返回 400（invalid size），脚本应视为致命错误、不换源
  auth401     返回 401（密钥失效），脚本应换源
  noimage     返回 200 但 data 里没有图片字段，脚本应换源
  ok_b64      返回 b64_json 图片，尺寸按请求的 size 生成
  ok_scene    返回 b64_json 水墨风山水图（有画面，演示视觉效果用）
  ok_url      返回图片 URL（GET /files/out.png 提供 777x555 图，故意与请求尺寸不同）
  url_dead    返回指向不存在资源的 URL，下载失败应换源
  edits_ok    仅 /v1/images/edits 返回 200；/v1/images/generations 返回 404
  model_down  请求模型为 gpt-image-2 时 500（模型过载），其他模型正常出图（测站内模型级联）
  model_404   请求模型为 gpt-image-2 时 404（模型不存在），其他模型正常（测站内模型级联）
  models_ok   GET /v1/models 返回混合模型列表（测模型发现过滤）
  ok_b64      提示词包含 FORCE_FAIL 时返回 500（测批量失败续跑），否则返回 b64_json 图片
"""
import base64
import json
import re
import struct
import threading
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _encode_png(w, h, raw_rows):
    """把带过滤字节的原始行打包成 PNG。"""
    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw_rows, 6)) + chunk(b"IEND", b""))


def make_png(w, h, rgb=(30, 144, 255)):
    """纯标准库生成一张纯色 PNG（8-bit RGB）。"""
    row = b"\x00" + bytes(rgb) * w
    return _encode_png(w, h, row * h)


def scene_png(w, h):
    """简易水墨风山水场景（渐变天空+落日+两层山+水面），用于演示视觉效果。"""
    horizon = int(h * 0.62)
    sky_top, sky_bot = (198, 208, 218), (244, 243, 238)
    far, near = (178, 186, 194), (108, 118, 130)
    water_top, water_bot = (222, 226, 224), (182, 192, 195)
    sun_x, sun_y, sun_r = 0.72 * w, 0.16 * h, 0.055 * h
    sun_col = (236, 215, 183)

    def ridge(cx, hw, amp):
        ys = {}
        for x in range(max(0, int((cx - hw) * w)), min(w, int((cx + hw) * w))):
            t = 1.0 - abs(x / w - cx) / hw
            ys[x] = horizon - int(amp * h * t)
        return ys

    r_far, r_near = ridge(0.36, 0.27, 0.20), ridge(0.60, 0.20, 0.13)
    rows = []
    for y in range(h):
        if y < horizon:
            t = y / horizon
            base = tuple(int(a + (b - a) * t) for a, b in zip(sky_top, sky_bot))
        else:
            t = (y - horizon) / max(1, h - horizon)
            base = tuple(int(a + (b - a) * t) for a, b in zip(water_top, water_bot))
        row = bytearray(bytes(base) * w)
        if y < horizon:
            dy = y - sun_y
            if abs(dy) < sun_r:  # 落日
                half = int((sun_r * sun_r - dy * dy) ** 0.5)
                for x in range(max(0, int(sun_x) - half), min(w, int(sun_x) + half)):
                    i = x * 3
                    row[i:i + 3] = bytes(sun_col)
            for x, ry in r_far.items():  # 远山，近山覆盖
                if ry <= y:
                    i = x * 3
                    row[i:i + 3] = bytes(far)
            for x, ry in r_near.items():
                if ry <= y:
                    i = x * 3
                    row[i:i + 3] = bytes(near)
        rows.append(b"\x00" + bytes(row))
    return _encode_png(w, h, b"".join(rows))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # 测试期间静默
        pass

    # ---- 基础响应 ----
    def _send_json(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_png(self, data):
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _request_size(self, body):
        m = re.search(rb'"size"\s*:\s*"?(\d{2,5})x(\d{2,5})', body)
        if m:
            return int(m.group(1)), int(m.group(2))
        m = re.search(rb'name="size"\r\n\r\n(\d{2,5})x(\d{2,5})', body)  # multipart 表单
        if m:
            return int(m.group(1)), int(m.group(2))
        return 64, 64

    def _field(self, body, name):
        """从 JSON 或 multipart 请求体中提取字段值（mock 简化实现）。"""
        m = re.search(('"%s"\\s*:\\s*"([^"]*)"' % name).encode(), body)
        if m:
            return m.group(1).decode("utf-8", "replace")
        m = re.search(('name="%s"\\r\\n\\r\\n([^\\r\\n]*)' % name).encode(), body)
        if m:
            return m.group(1).decode("utf-8", "replace")
        return ""

    # ---- 业务 ----
    def do_POST(self):
        body = self._read_body()
        auth = self.headers.get("Authorization") or ""
        self.server.requests.append({
            "path": self.path,
            "auth": auth,
            "has_image_part": b"\x89PNG" in body or b"\xff\xd8\xff" in body,
            "body_len": len(body),
        })
        if not auth.startswith("Bearer "):
            self._send_json(401, {"error": {"message": "missing bearer token (mock)"}})
            return
        mode = self.server.mode
        model = self._field(body, "model")
        prompt = self._field(body, "prompt")
        w, h = self._request_size(body)
        png = make_png(w, h)
        b64 = base64.b64encode(png).decode("ascii")

        if mode == "fail500":
            self._send_json(500, {"error": {"message": "upstream overload (mock)"}})
        elif mode == "echoauth500":
            self._send_json(500, {"error": {"message": "rejected, your key is %s (mock)" % auth}})
        elif mode == "bad400":
            self._send_json(400, {"error": {"message": "invalid size for this model (mock)"}})
        elif mode == "auth401":
            self._send_json(401, {"error": {"message": "invalid api key (mock)"}})
        elif mode == "noimage":
            self._send_json(200, {"created": 0, "data": [{"revised_prompt": "nothing"}]})
        elif mode == "ok_b64":
            if "FORCE_FAIL" in prompt:  # 供批量用例制造单条失败
                self._send_json(500, {"error": {"message": "forced fail (mock)"}})
            else:
                self._send_json(200, {"created": 0, "model": "mock-image-model",
                                      "data": [{"b64_json": b64}]})
        elif mode == "model_down":  # 站内模型级联：gpt-image-2 过载，其他模型正常
            if model == "gpt-image-2":
                self._send_json(500, {"error": {"message": "model overloaded (mock)"}})
            else:
                self._send_json(200, {"created": 0, "model": "mock-image-model",
                                      "data": [{"b64_json": b64}]})
        elif mode == "model_404":  # 站内模型级联：gpt-image-2 不存在，其他模型正常
            if model == "gpt-image-2":
                self._send_json(404, {"error": {"message": "unknown model (mock)"}})
            else:
                self._send_json(200, {"created": 0, "model": "mock-image-model",
                                      "data": [{"b64_json": b64}]})
        elif mode == "ok_scene":
            self._send_json(200, {"created": 0, "model": "mock-image-model",
                                  "data": [{"b64_json": base64.b64encode(
                                      scene_png(w, h)).decode("ascii")}]})
        elif mode == "ok_url":
            port = self.server.server_address[1]
            self._send_json(200, {"created": 0, "model": "mock-image-model",
                                  "data": [{"url": "http://127.0.0.1:%d/files/out.png" % port}]})
        elif mode == "url_dead":
            port = self.server.server_address[1]
            self._send_json(200, {"data": [{"url": "http://127.0.0.1:%d/files/missing.png" % port}]})
        elif mode == "edits_ok":
            if self.path.rstrip("/").endswith("/images/edits"):
                self._send_json(200, {"created": 0, "data": [{"b64_json": b64}]})
            else:
                self._send_json(404, {"error": {"message": "generations not offered here (mock)"}})
        else:
            self._send_json(500, {"error": {"message": "unknown mock mode %r" % mode}})

    def do_GET(self):
        if self.path.rstrip("/").endswith("/models") and self.server.mode == "models_ok":
            self._send_json(200, {"object": "list", "data": [
                {"id": "gpt-image-2"}, {"id": "gpt-image-2.5-flare"},
                {"id": "dall-e-3"}, {"id": "flux-1.1-pro"},
                {"id": "gpt-4o"}, {"id": "text-embedding-3"},
            ]})
            return
        if self.path == "/files/out.png":
            self._send_png(self.server.url_png)  # 固定 777x555，与请求尺寸故意不一致
        else:
            self._send_json(404, {"error": {"message": "not found (mock)"}})


def start_mock(mode):
    """启动一个模拟中转站，返回 ThreadingHTTPServer（已在线程中运行）。"""
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.mode = mode
    server.requests = []
    server.url_png = make_png(777, 555, (200, 60, 60))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
