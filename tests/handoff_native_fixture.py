"""Loopback-only synthetic Responses server for isolated native Codex tests.

Never use real credentials or a production CODEX_HOME. This is test data, not
a model-quality test and not a real Provider integration.
"""
import json
import threading
import uuid
import sys
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class LoopbackResponses:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.requests = []
        self.payloads = []
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
                fixture.payloads.append(payload)
                fixture.requests.append(self.path)
                if not fixture.outputs:
                    self.send_error(400)
                    return
                output = fixture.outputs.pop(0)
                output = output(payload) if callable(output) else output
                text = json.dumps(output, ensure_ascii=False)
                ident = "msg_" + uuid.uuid4().hex
                item = {"id": ident, "type": "message", "status": "completed", "role": "assistant",
                        "phase": "final_answer", "content": [{"type": "output_text", "text": text, "annotations": []}]}
                response = {"id": "resp_" + uuid.uuid4().hex, "object": "response", "created_at": 1700000000,
                            "status": "completed", "model": "gpt-5.6-sol", "output": [item],
                            "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2,
                                      "input_tokens_details": {"cached_tokens": 0},
                                      "output_tokens_details": {"reasoning_tokens": 0}}}
                events = [
                    {"type": "response.created", "response": {**response, "status": "in_progress", "output": []}},
                    {"type": "response.output_item.added", "output_index": 0, "item": {**item, "status": "in_progress", "content": []}},
                    {"type": "response.content_part.added", "item_id": ident, "output_index": 0, "content_index": 0,
                     "part": {"type": "output_text", "text": "", "annotations": []}},
                    {"type": "response.output_text.delta", "item_id": ident, "output_index": 0, "content_index": 0, "delta": text},
                    {"type": "response.output_text.done", "item_id": ident, "output_index": 0, "content_index": 0, "text": text},
                    {"type": "response.output_item.done", "output_index": 0, "item": item},
                    {"type": "response.completed", "response": response},
                ]
                if isinstance(output, dict) and "__fixture_tool_call__" in output:
                    call = {"id": ident, "type": "function_call", "status": "completed",
                            "call_id": "call_" + uuid.uuid4().hex, **output["__fixture_tool_call__"]}
                    events = [
                        {"type": "response.created", "response": {**response, "status": "in_progress", "output": []}},
                        {"type": "response.output_item.added", "output_index": 0, "item": call},
                        {"type": "response.output_item.done", "output_index": 0, "item": call},
                        {"type": "response.completed", "response": {**response, "output": [call]}},
                    ]
                body = "".join("data: " + json.dumps(event) + "\n\n" for event in events).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.server.server_port}/v1"

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)


def mcp_fixture(marker):
    Path(marker).write_text("connected", encoding="utf-8")
    for line in sys.stdin:
        message = json.loads(line)
        if "id" not in message:
            continue
        method = message.get("method")
        if method == "initialize":
            result = {"protocolVersion": message.get("params", {}).get("protocolVersion", "2024-11-05"),
                      "capabilities": {"tools": {}}, "serverInfo": {"name": "fixture-reader", "version": "1"}}
        elif method == "tools/list":
            result = {"tools": [{"name": "fixture_read", "description": "Read-only offline fixture",
                                 "inputSchema": {"type": "object", "properties": {}},
                                 "annotations": {"readOnlyHint": True, "destructiveHint": False}}]}
        elif method == "resources/list":
            result = {"resources": []}
        elif method == "resources/templates/list":
            result = {"resourceTemplates": []}
        else:
            result = {}
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)


if __name__ == "__main__" and sys.argv[1:2] == ["--mcp-server"]:
    mcp_fixture(sys.argv[2])
