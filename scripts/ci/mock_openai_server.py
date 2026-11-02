"""Tiny OpenAI-compatible server for CI smoke tests (no real model).

Usage: python mock_openai_server.py <port>
"""
import json, sys, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
REPLY = "Hello from your PC! This answer came from Ghost."
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _json(self, obj, code=200):
        b = json.dumps(obj).encode(); self.send_response(code)
        self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(b)))
        self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        if self.path.rstrip("/").endswith("/models"):
            return self._json({"object": "list", "data": [{"id": "mock-model", "object": "model", "owned_by": "me"}]})
        self._json({"ok": True})
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0)) or 0) or b"{}")
        if body.get("stream"):
            self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
            for w in REPLY.split(" "):
                chunk = {"id": "x", "object": "chat.completion.chunk", "created": int(time.time()), "model": "mock-model",
                         "choices": [{"index": 0, "delta": {"content": w + " "}, "finish_reason": None}]}
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode()); self.wfile.flush()
            end = {"id": "x", "object": "chat.completion.chunk", "created": int(time.time()), "model": "mock-model",
                   "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                   "usage": {"prompt_tokens": 5, "completion_tokens": 9, "total_tokens": 14}}
            self.wfile.write(f"data: {json.dumps(end)}\n\ndata: [DONE]\n\n".encode()); return
        self._json({"id": "x", "object": "chat.completion", "created": int(time.time()), "model": "mock-model",
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": REPLY}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 9, "total_tokens": 14}})
ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
