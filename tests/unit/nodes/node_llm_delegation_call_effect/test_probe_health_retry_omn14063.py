# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Regression tests: ``probe_health`` retries before declaring unhealthy (OMN-14063).

Found via dogfood of the ``delegate`` skill (2026-07-06): a single-shot 5s health
probe over Tailscale intermittently timed out against a backend that was live
throughout (live repro: 4/5 quick curls succeeded, 1/5 hit the 5.0s ceiling). A
transport failure_class=MODEL_UNAVAILABLE from a false-negative probe is treated
identically to a genuinely dead backend by the escalation ladder — silently
routing LOCAL-FIRST work to a paid cloud tier and burning real API cost for zero
quality gain. These tests pin the fix: a transient single-attempt failure must
not flip the probe to unhealthy when a retry would have succeeded, and a
genuinely-dead endpoint must still resolve to unhealthy (never masked forever).
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from omnimarket.nodes.node_llm_delegation_call_effect.handlers import transport

_MACOS_PROFILE = "local_macos_claude_hooks"


@pytest.mark.unit
def test_transient_probe_failure_recovers_on_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A single failed attempt followed by a healthy one must return True.

    Mirrors the live repro exactly: the 1st probe times out (transient blip),
    the 2nd succeeds — the endpoint was never actually down.
    """
    calls: list[int] = []

    def flaky_httpx_probe(_probe_url: str, *, timeout_seconds: float) -> bool:
        calls.append(1)
        return len(calls) >= 2  # fails once, then succeeds

    monkeypatch.setattr(transport, "_httpx_probe_health", flaky_httpx_probe)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    healthy = transport.probe_health(
        "http://inference.example:8000/v1/chat/completions",
        runtime_profile="effects",
    )

    assert healthy is True
    assert len(calls) == 2


@pytest.mark.unit
def test_probe_returns_true_immediately_without_retry_on_first_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The common case (healthy on the first try) makes exactly one call."""
    calls: list[int] = []

    def always_ok(_probe_url: str, *, timeout_seconds: float) -> bool:
        calls.append(1)
        return True

    slept = []
    monkeypatch.setattr(transport, "_httpx_probe_health", always_ok)
    monkeypatch.setattr(time, "sleep", lambda seconds: slept.append(seconds))

    healthy = transport.probe_health(
        "http://inference.example:8000/v1/chat/completions",
        runtime_profile="effects",
    )

    assert healthy is True
    assert len(calls) == 1
    assert slept == []  # no backoff needed when the first attempt succeeds


@pytest.mark.unit
def test_genuinely_dead_endpoint_still_resolves_unhealthy_after_all_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A backend that fails every attempt must still resolve to unhealthy.

    The retry is a false-negative guard, not a way to mask a real outage —
    exhausting all attempts must still return False.
    """
    calls: list[int] = []

    def always_down(_probe_url: str, *, timeout_seconds: float) -> bool:
        calls.append(1)
        return False

    monkeypatch.setattr(transport, "_httpx_probe_health", always_down)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    healthy = transport.probe_health(
        "http://inference.example:8000/v1/chat/completions",
        runtime_profile="effects",
        attempts=3,
    )

    assert healthy is False
    assert len(calls) == 3


@pytest.mark.unit
def test_curl_transport_also_retries_on_macos_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The LAN-safe curl transport gets the same retry-before-unhealthy fix."""
    calls: list[int] = []

    def flaky_curl_probe(_probe_url: str, *, timeout_seconds: float) -> bool:
        calls.append(1)
        return len(calls) >= 2

    monkeypatch.setattr(transport, "_curl_probe_health", flaky_curl_probe)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    healthy = transport.probe_health(
        "http://inference.example:8000/v1/chat/completions",
        runtime_profile=_MACOS_PROFILE,
    )

    assert healthy is True
    assert len(calls) == 2


@pytest.mark.unit
def test_backoff_sleep_called_between_failed_attempts_not_after_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Backoff only happens BETWEEN attempts, never after the final result."""
    calls: list[int] = []

    def flaky(_probe_url: str, *, timeout_seconds: float) -> bool:
        calls.append(1)
        return len(calls) >= 2

    slept: list[float] = []
    monkeypatch.setattr(transport, "_httpx_probe_health", flaky)
    monkeypatch.setattr(time, "sleep", lambda seconds: slept.append(seconds))

    transport.probe_health(
        "http://inference.example:8000/v1/chat/completions",
        runtime_profile="effects",
        backoff_seconds=0.25,
    )

    assert slept == [0.25]  # exactly one backoff pause, between attempt 1 and 2


@pytest.mark.unit
def test_probe_health_fails_closed_on_non_http_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-http(s) endpoints fail closed before any probe attempt (unchanged)."""

    def fail(*_args: Any, **_kwargs: Any) -> bool:
        raise AssertionError("must not probe a non-http(s) URL")

    monkeypatch.setattr(transport, "_httpx_probe_health", fail)
    monkeypatch.setattr(transport, "_curl_probe_health", fail)

    with pytest.raises(ValueError, match="COMPLETE resolved http"):
        transport.probe_health("ftp://host/health", runtime_profile="effects")
