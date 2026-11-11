# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HTTP catalog size controls: oversized success/error and compressed bodies.

An unbounded provider response must not become a partial, apparently valid
catalog. Ordinary catalogs and bounded authentication errors retain their shape.
"""

import gzip
import json
import threading
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from omnimarket.nodes.node_llm_delegation_call_effect.handlers.transport import (
    get_provider_json,
)

pytestmark = pytest.mark.e2e


@pytest.mark.parametrize(
    ("status", "compressed"), [(200, False), (401, False), (200, True)]
)
def test_oversized_catalog_refused(
    tmp_path: Path, status: int, compressed: bool
) -> None:
    _exercise_catalog(tmp_path, status=status, compressed=compressed, oversized=True)


@pytest.mark.parametrize("status", [200, 401])
def test_bounded_catalog_and_auth_error(tmp_path: Path, status: int) -> None:
    _exercise_catalog(tmp_path, status=status, compressed=False, oversized=False)


def _exercise_catalog(
    tmp_path: Path, *, status: int, compressed: bool, oversized: bool
) -> None:
    payload = {"data": [{"id": "future-model"}], "message": "authentication required"}
    if oversized:
        payload["padding"] = "x" * (8 * 1024 * 1024)
    decoded = json.dumps(payload).encode()
    body = gzip.compress(decoded) if compressed else decoded
    requests: list[str] = []

    class Catalog(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            requests.append(self.path)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            if compressed:
                self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            # A bounded reader deliberately closes an oversized stream.
            with suppress(BrokenPipeError, ConnectionResetError):
                self.wfile.write(body)

        def log_message(self, *_args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Catalog)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/models"
        if oversized:
            with pytest.raises(ValueError, match="response exceeds"):
                get_provider_json(url=url, timeout_seconds=5)
        elif status == 401:
            with pytest.raises(httpx.HTTPStatusError) as error:
                get_provider_json(url=url, timeout_seconds=5)
            assert error.value.response.status_code == status
            assert error.value.response.json() == payload
        else:
            assert get_provider_json(url=url, timeout_seconds=5) == payload
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert requests == ["/models"]
    (tmp_path / "catalog-bound.json").write_text(
        json.dumps(
            {
                "requests": requests,
                "status": status,
                "compressed": compressed,
                "decoded_bytes": len(decoded),
                "oversized_refused": oversized,
            }
        )
    )
