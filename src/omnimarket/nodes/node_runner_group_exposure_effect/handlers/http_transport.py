# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The real HTTP getter (the contract declares the http transport): urllib, one GET, status and body back (an HTTP error is a status)."""

from __future__ import annotations

import urllib.error
import urllib.request
from collections.abc import Mapping


class UrllibHttpTransport:
    def get(
        self, url: str, headers: Mapping[str, str], timeout: float
    ) -> tuple[int, bytes]:
        request = urllib.request.Request(url, headers=dict(headers), method="GET")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()
        except (urllib.error.URLError, TimeoutError, OSError):
            return 599, b""
