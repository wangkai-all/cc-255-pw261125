"""HTTP 层：纯标准库 http.server，提供 JSON API 与静态页面。

路由：
  POST /api/receive                 接收回传（存证+收据+入队处理）
  GET  /api/snapshot/<response_id>  工作台/设备完整视图
  GET  /api/receipts/<response_id>  全部收据与原始存证
  GET  /api/queue/<response_id>     操作队列（FIFO 与阻断依据）
  GET  /api/audit                   审计日志
  POST /api/conflicts/<id>/resolve  人工裁决
  POST /api/publish/<response_id>   发布（带门禁）
  POST /api/reset                   重置演示数据
  GET  /api/blob/<response_id>/<attachment_id>  下载附件原文
  GET  /device.html、/workbench.html、/static/* 页面与资源
"""
from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .engine import Engine
from .store import Store

STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
}


class App:
    def __init__(self, db_path: str | None = None, blob_dir: str | None = None):
        db_path = db_path or os.path.join(DATA_DIR, "survey.db")
        blob_dir = blob_dir or os.path.join(DATA_DIR, "blobs")
        self.store = Store(db_path, blob_dir)
        self.engine = Engine(self.store)
        self.engine.seed()
        # 进程重启后重放未处理任务
        self.engine.replay_pending()


def make_handler(app: App):
    engine = app.engine

    class Handler(BaseHTTPRequestHandler):
        server_version = "OfflineSurvey/1.0"

        def log_message(self, fmt, *args):  # 精简日志
            pass

        def _json(self, obj, status: int = 200):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict | None:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b""
            try:
                return json.loads(raw.decode("utf-8"))
            except Exception:
                return {"__parse_error__": raw.decode("utf-8", errors="replace")}

        def _static(self, rel: str):
            # 防路径穿越
            safe = os.path.normpath(rel).lstrip("/")
            full = os.path.join(STATIC_DIR, safe)
            if not full.startswith(STATIC_DIR) or not os.path.isfile(full):
                self.send_error(404)
                return
            ext = os.path.splitext(full)[1]
            with open(full, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", CONTENT_TYPES.get(ext, "application/octet-stream"))
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            try:
                if path in ("/", "/workbench.html"):
                    return self._static("workbench.html")
                if path == "/device.html":
                    return self._static("device.html")
                if path.startswith("/static/"):
                    return self._static(path[len("/static/"):])
                if path.startswith("/api/snapshot/"):
                    return self._json(engine.snapshot(path.rsplit("/", 1)[1]))
                if path.startswith("/api/receipts/"):
                    return self._json(engine.receipts(path.rsplit("/", 1)[1]))
                if path.startswith("/api/queue/"):
                    return self._json(engine.queue(path.rsplit("/", 1)[1]))
                if path == "/api/audit":
                    return self._json(engine.audit_log())
                if path.startswith("/api/blob/"):
                    parts = path.split("/")
                    # /api/blob/<response_id>/<attachment_id>
                    _, _, _, response_id, attachment_id = parts[:5]
                    got = engine.attachment_bytes(response_id, attachment_id)
                    if got is None:
                        return self.send_error(404, "附件不存在或已删除")
                    file_name, _digest, content = got
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Disposition",
                                     f'attachment; filename="{file_name}"')
                    self.send_header("Content-Length", str(len(content)))
                    self.end_headers()
                    return self.wfile.write(content)
                self.send_error(404)
            except Exception as exc:
                self._json({"error": str(exc)}, 500)

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            try:
                body = self._read_json()
                if path == "/api/receive":
                    if "__parse_error__" in (body or {}):
                        # 即便 JSON 解析失败，也给一张收据式存证（invalid_json）
                        return self._json(engine.receive({"_raw": body["__parse_error__"]}))
                    return self._json(engine.receive(body or {}))
                if path == "/api/reset":
                    return self._json(engine.reset())
                if path.startswith("/api/publish/"):
                    return self._json(engine.publish(path.rsplit("/", 1)[1]))
                if path.startswith("/api/conflicts/") and path.endswith("/resolve"):
                    conflict_id = path.split("/")[3]
                    return self._json(engine.resolve_conflict(
                        conflict_id,
                        resolution=(body or {}).get("resolution", ""),
                        chosen_value=(body or {}).get("chosen_value"),
                        chosen_attachment_id=(body or {}).get("chosen_attachment_id"),
                        restore_response=bool((body or {}).get("restore_response")),
                    ))
                self.send_error(404)
            except Exception as exc:
                self._json({"error": str(exc)}, 500)

    return Handler


def serve(host: str = "127.0.0.1", port: int = 8000) -> None:
    app = App()
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    print(f"离线答卷回传与冲突裁决系统已启动: http://{host}:{port}")
    print(f"  设备 A : http://{host}:{port}/device.html?device=A")
    print(f"  设备 B : http://{host}:{port}/device.html?device=B")
    print(f"  工作台 : http://{host}:{port}/workbench.html")
    httpd.serve_forever()


if __name__ == "__main__":
    serve()
