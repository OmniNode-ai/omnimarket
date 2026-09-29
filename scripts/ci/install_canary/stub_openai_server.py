# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A loopback-only, deterministic OpenAI server for the install canary."""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock

MODEL = "canary-stub-model"


def serve(port: int) -> ThreadingHTTPServer:
    """Bind a server; the caller owns serve_forever and shutdown."""
    lock = Lock()
    request_count = 0

    class Handler(BaseHTTPRequestHandler):
        def respond(self, payload: dict[str, object], status: int = 200) -> None:
            nonlocal request_count
            with lock:
                request_count += 1
                logging.info(
                    "request_count=%d %s %s", request_count, self.command, self.path
                )
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path == "/v1/models":
                self.respond(
                    {"object": "list", "data": [{"id": MODEL, "object": "model"}]}
                )
            else:
                self.respond({"error": "not found"}, 404)

        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            if self.path != "/v1/chat/completions":
                self.respond({"error": "not found"}, 404)
                return
            self.respond(
                {
                    "id": "chatcmpl-canary",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": MODEL,
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "canary-ok"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 6,
                        "completion_tokens": 3,
                        "total_tokens": 9,
                    },
                }
            )

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--port-file", type=Path)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    with serve(args.port) as server:
        if args.port_file is not None:
            args.port_file.write_text(str(server.server_port), encoding="utf-8")
        logging.info("stub listening on port %d", server.server_port)
        with contextlib.suppress(KeyboardInterrupt):
            server.serve_forever()


if __name__ == "__main__":
    main()
