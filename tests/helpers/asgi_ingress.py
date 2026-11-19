# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Exercise an ASGI application in-process without any network HTTP client.

The supplied application owns upstream model transport. This harness invokes
only its ASGI scope/receive/send interface and retains the complete response.
"""

import httpx


class AsgiIngress:
    def __init__(self, app):
        self._transport = httpx.ASGITransport(app)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        await self._transport.aclose()

    async def invoke(self, method, path, **kwargs):
        request = httpx.Request(method, "http://asgi.invalid" + path, **kwargs)
        response = await self._transport.handle_async_request(request)
        response.request = request
        await response.aread()
        return response
