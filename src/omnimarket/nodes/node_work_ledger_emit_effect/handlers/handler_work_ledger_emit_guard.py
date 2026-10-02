# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The work-ledger emit guard: a test process never publishes to a real topic (OMN-19513).

Operator ruling 2026-10-01: "it should be impossible to like fake test like that". A test
that drives ``HandlerWorkLedgerEmit`` with a real broker in its environment would publish
fixture rows (``lane=alpha ticket=OMN-1``) to the real ``work.ledger.*`` topics, and the
projection would fold them into ``work_ledger_rows``. Isolation by test setup is a
convention a test can forget; this guard sits in the publish path.

THE RULE. An emit is refused when BOTH hold:

1. The process runs under a test runner: ``PYTEST_CURRENT_TEST`` is set, ``pytest`` or
   ``unittest`` is imported, or ``ONEX_TEST_CONTEXT`` is set to any non-empty value. No
   value of ``ONEX_TEST_CONTEXT`` removes a signal.
2. The bus the emit would publish through is real: ``KAFKA_BOOTSTRAP_SERVERS`` names a
   host that is not loopback, and the spool-only opt-out is not set (spool-only publishes
   nothing).

The handler judges this before it types the row, builds an emitter, connects or publishes.
A test emits through a loopback or unset bootstrap, or the spool-only lane. There is no
bypass flag and no allowlist. The command form,
``python -m omnimarket.nodes.node_work_ledger_emit_effect.handlers.handler_work_ledger_emit_guard``,
exits 0 when the emit may proceed and 79 with the refusal on stderr when it may not.
"""

from __future__ import annotations

import argparse
import os
import sys

GUARD_NAME = "ledger-test-write-guard"
TEST_CONTEXT_ENV = "ONEX_TEST_CONTEXT"
BOOTSTRAP_ENV = "KAFKA_BOOTSTRAP_SERVERS"
SPOOL_ONLY_ENV = "ONEX_EMIT_EFFECT_SPOOL_ONLY"
EXIT_TEST_WRITE_REFUSED = 79
_LOOPBACK_HOSTS = frozenset({"", "localhost", "127.0.0.1", "::1"})
_SPOOL_ONLY_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_RUNNER_MODULES = ("pytest", "unittest")


class LedgerTestWriteRefusedError(Exception):
    """A test process tried to publish to a real work-ledger topic."""


class HandlerWorkLedgerEmitGuard:
    """Judges the emit's bus target against the test-runner signals."""

    @staticmethod
    def test_context() -> str | None:
        """The first test-runner signal present, named, or None outside a test."""
        if os.environ.get("PYTEST_CURRENT_TEST"):
            return "PYTEST_CURRENT_TEST is set"
        if os.environ.get(TEST_CONTEXT_ENV, "").strip():
            return f"{TEST_CONTEXT_ENV} is set"
        for name in _RUNNER_MODULES:
            if name in sys.modules:
                return f"{name} is imported"
        return None

    @staticmethod
    def _server_host(server: str) -> str:
        text = server.strip().removeprefix("PLAINTEXT://").removeprefix("SSL://")
        if text.startswith("["):
            return text[1:].split("]", 1)[0]
        return text.rsplit(":", 1)[0] if text.count(":") == 1 else text

    @classmethod
    def real_bus_host(cls) -> str | None:
        """The first non-loopback broker host the emit would publish to, else None."""
        if (
            os.environ.get(SPOOL_ONLY_ENV, "").strip().lower()
            in _SPOOL_ONLY_TRUE_VALUES
        ):
            return None
        for server in os.environ.get(BOOTSTRAP_ENV, "").split(","):
            host = cls._server_host(server).lower()
            if host not in _LOOPBACK_HOSTS:
                return host
        return None

    @classmethod
    def refusal(cls) -> str | None:
        """The refusal message for a test process emitting to a real bus, else None."""
        signal = cls.test_context()
        if signal is None:
            return None
        host = cls.real_bus_host()
        if host is None:
            return None
        return (
            f"{GUARD_NAME} REFUSED -- a test process ({signal}) tried to publish to the "
            f"real work-ledger topics (broker host {host}). Nothing was published. A test "
            "emits through an in-memory bus, a loopback broker or the spool-only lane "
            "(node_work_ledger_emit_effect, OMN-19513)."
        )

    @classmethod
    def check(cls) -> None:
        """Raise LedgerTestWriteRefusedError naming the guard when a test targets a real bus."""
        message = cls.refusal()
        if message is not None:
            raise LedgerTestWriteRefusedError(message)


def main(argv: list[str] | None = None) -> int:
    """The command form: exit 0 when the emit may proceed, 79 when a test may not."""
    argparse.ArgumentParser(
        description="Refuse a test process's publish to a real work-ledger topic."
    ).parse_args(argv)
    message = HandlerWorkLedgerEmitGuard.refusal()
    if message is not None:
        sys.stderr.write(message + "\n")
        return EXIT_TEST_WRITE_REFUSED
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
