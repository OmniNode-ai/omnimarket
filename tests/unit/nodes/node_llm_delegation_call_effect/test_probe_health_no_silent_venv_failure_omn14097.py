# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Regression: a broken/mismatched venv must never silently escalate to paid cloud (OMN-14097).

Root cause (2026-07-07 dogfood): running ``onex delegate`` from omnimarket's own
venv hit a stale ``omnibase-spi`` import gap. The exact import error is now
pinned at the dependency level (OMN-14003), but the *architectural* hole it
exposed was never closed: ``transport._httpx_probe_health`` caught a bare
``except Exception`` around the health probe and returned ``False`` (i.e.
"unhealthy, escalate") for ANY exception — including a programming/environment
defect (``ImportError``, ``AttributeError``, ``TypeError``, ``NameError``) that
has nothing to do with the endpoint actually being unreachable. That conflation
is exactly what makes a broken venv indistinguishable from a genuinely-down
local backend: both silently skip the local (free) tier and escalate to a paid
cloud tier with a fully "successful" (``exit 0``) result.

These tests pin the fix: a genuine network/transport failure (connection
refused, DNS failure, timeout) must still resolve the probe to unhealthy
(unchanged behavior — OMN-14063's retry-before-unhealthy guard stays intact),
but a non-network exception class (the signature of a broken/mismatched venv,
not a reachability problem) must propagate instead of being silently absorbed
as "unhealthy" — so the caller fails loud instead of quietly paying for cloud.
"""

from __future__ import annotations

import httpx
import pytest

from omnimarket.nodes.node_llm_delegation_call_effect.handlers import transport


@pytest.mark.unit
def test_import_error_in_health_probe_propagates_not_swallowed_as_unhealthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale-venv-class ImportError during the probe must NOT resolve to False.

    Simulates the OMN-14097 root neighborhood: a broken/mismatched venv (stale
    omnibase-spi) raises ``ImportError`` deep inside probe machinery. Before the
    fix, ``_httpx_probe_health``'s bare ``except Exception`` swallowed this and
    returned ``False`` — indistinguishable from a genuinely-dead local backend,
    silently routing the caller to a paid escalation. The fix must let this
    class of exception propagate so it surfaces as a hard failure instead.
    """

    class _ExplodingClient:
        def __enter__(self) -> _ExplodingClient:
            return self

        def __exit__(self, *_exc_info: object) -> None:
            return None

        def get(self, *_args: object, **_kwargs: object) -> object:
            raise ImportError(
                "cannot import name 'ProtocolDispatchEngine' from "
                "'omnibase_spi.protocols.runtime'"
            )

    monkeypatch.setattr(httpx, "Client", lambda *_args, **_kwargs: _ExplodingClient())

    with pytest.raises(ImportError, match="ProtocolDispatchEngine"):
        transport.probe_health(
            "http://100.109.203.94:8000/v1/chat/completions",
            runtime_profile="effects",
            attempts=1,
        )


@pytest.mark.unit
def test_attribute_error_in_health_probe_propagates_not_swallowed_as_unhealthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A version-mismatch-class AttributeError must also propagate, not swallow."""

    class _ExplodingClient:
        def __enter__(self) -> _ExplodingClient:
            return self

        def __exit__(self, *_exc_info: object) -> None:
            return None

        def get(self, *_args: object, **_kwargs: object) -> object:
            raise AttributeError(
                "module 'omnibase_spi.protocols.runtime' has no attribute "
                "'ProtocolDispatchEngine'"
            )

    monkeypatch.setattr(httpx, "Client", lambda *_args, **_kwargs: _ExplodingClient())

    with pytest.raises(AttributeError, match="ProtocolDispatchEngine"):
        transport.probe_health(
            "http://100.109.203.94:8000/v1/chat/completions",
            runtime_profile="effects",
            attempts=1,
        )


@pytest.mark.unit
def test_genuine_connection_failure_still_resolves_unhealthy_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real network failure (ConnectError) must still resolve to False.

    Guards against over-correcting: only non-network exception classes should
    propagate. A genuine transport failure is the correct, intentional trigger
    for escalation and must keep working exactly as before (OMN-14063).
    """

    class _RefusingClient:
        def __enter__(self) -> _RefusingClient:
            return self

        def __exit__(self, *_exc_info: object) -> None:
            return None

        def get(self, *_args: object, **_kwargs: object) -> object:
            raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "Client", lambda *_args, **_kwargs: _RefusingClient())

    healthy = transport.probe_health(
        "http://100.109.203.94:8000/v1/chat/completions",
        runtime_profile="effects",
        attempts=1,
    )
    assert healthy is False


@pytest.mark.unit
def test_genuine_os_error_still_resolves_unhealthy_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raw OSError (e.g. EHOSTUNREACH escaping httpx) must still resolve False.

    OMN-13160 documents EHOSTUNREACH as the exact silent failure mode when an
    adhoc-signed uv Python lacks the macOS Local Network grant. That is a real
    reachability signal (from THIS process's transport, not a code defect) and
    must keep escalating exactly as before — only non-network exception classes
    are the new propagate-loud case.
    """

    class _UnreachableClient:
        def __enter__(self) -> _UnreachableClient:
            return self

        def __exit__(self, *_exc_info: object) -> None:
            return None

        def get(self, *_args: object, **_kwargs: object) -> object:
            raise OSError("[Errno 65] No route to host")

    monkeypatch.setattr(httpx, "Client", lambda *_args, **_kwargs: _UnreachableClient())

    healthy = transport.probe_health(
        "http://100.109.203.94:8000/v1/chat/completions",
        runtime_profile="effects",
        attempts=1,
    )
    assert healthy is False
