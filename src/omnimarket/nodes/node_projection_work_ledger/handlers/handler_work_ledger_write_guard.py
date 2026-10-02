# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The work-ledger projection write guard: a test process never writes the real DSN (OMN-19513).

Operator ruling 2026-10-01: "it should be impossible to like fake test like that". A test
that drives ``WorkLedgerProjectionWriter`` with a real database URL in its environment
would insert fixture rows (``lane=alpha ticket=OMN-1``) into ``omninode_internal.work_ledger_rows``.
Isolation by test setup is a convention a test can forget; this guard sits in the write path.

THE RULE. A write is refused when BOTH hold:

1. The process runs under a test runner: ``PYTEST_CURRENT_TEST`` is set (pytest sets it
   for every test's setup, call and teardown, and a subprocess inherits it), or
   ``ONEX_TEST_CONTEXT`` is set to any non-empty value. No value of ``ONEX_TEST_CONTEXT``
   removes a signal. A module merely being imported is not a signal: the long-lived
   runtime imports pytest transitively, and treating that as a test refused every
   production write and, through SystemExit, restarted the runtime every few minutes
   (OMN-17427).
2. The DSN the writer would connect with is real: its host is not loopback and not a local
   socket.

The writer calls :meth:`HandlerWorkLedgerWriteGuard.check_dsn` before it connects and before
it executes a statement. A refusal raises ``LedgerTestWriteRefusedError``, an ordinary
handler error, never ``SystemExit``, so a refusal can fail a test or one dispatch but cannot
stop a host process. A test writes through a loopback DSN or an injected fake database.
There is no bypass flag and no allowlist. The command form,
``python -m omnimarket.nodes.node_projection_work_ledger.handlers.handler_work_ledger_write_guard --dsn <dsn>``,
exits 0 when the write may proceed and 79 with the refusal on stderr when it may not.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from urllib.parse import urlsplit

GUARD_NAME = "ledger-test-write-guard"
TEST_CONTEXT_ENV = "ONEX_TEST_CONTEXT"
EXIT_TEST_WRITE_REFUSED = 79
_LOOPBACK_HOSTS = frozenset({"", "localhost", "127.0.0.1", "::1"})


class LedgerTestWriteRefusedError(Exception):
    """A test process tried to write the real work_ledger_rows DSN."""


class HandlerWorkLedgerWriteGuard:
    """Judges a projection DSN against the test-runner signals."""

    @staticmethod
    def test_context() -> str | None:
        """The first test-runner signal present, named, or None outside a test."""
        if os.environ.get("PYTEST_CURRENT_TEST"):
            return "PYTEST_CURRENT_TEST is set"
        if os.environ.get(TEST_CONTEXT_ENV, "").strip():
            return f"{TEST_CONTEXT_ENV} is set"
        return None

    @staticmethod
    def dsn_host(dsn: str) -> str:
        text = dsn.strip()
        if "://" in text:
            return urlsplit(text).hostname or ""
        match = re.search(r"(?:^|\s)host=(\S+)", text)
        return match.group(1) if match else ""

    @classmethod
    def dsn_is_real(cls, dsn: str) -> bool:
        """A DSN reaches a shared database unless its host is loopback or a local socket."""
        host = cls.dsn_host(dsn).lower()
        return not (host in _LOOPBACK_HOSTS or host.startswith("/"))

    @classmethod
    def refusal(cls, dsn: str) -> str | None:
        """The refusal message for a test process writing a real DSN, else None."""
        signal = cls.test_context()
        if signal is None or not dsn.strip() or not cls.dsn_is_real(dsn):
            return None
        host = cls.dsn_host(dsn) or "<non-loopback host>"
        return (
            f"{GUARD_NAME} REFUSED -- a test process ({signal}) tried to write the real "
            f"work_ledger_rows DSN (host {host}). Nothing was written. A test writes a "
            "loopback DSN or an injected fake database "
            "(node_projection_work_ledger, OMN-19513)."
        )

    @classmethod
    def check_dsn(cls, dsn: str) -> None:
        """Raise LedgerTestWriteRefusedError when a test process holds a real DSN."""
        message = cls.refusal(dsn)
        if message is not None:
            raise LedgerTestWriteRefusedError(message)


def main(argv: list[str] | None = None) -> int:
    """The command form: exit 0 when the DSN may be written, 79 when a test may not."""
    parser = argparse.ArgumentParser(
        description="Refuse a test process's real-DSN write."
    )
    parser.add_argument("--dsn", action="append", default=[])
    args = parser.parse_args(argv)
    for dsn in args.dsn:
        message = HandlerWorkLedgerWriteGuard.refusal(dsn)
        if message is not None:
            sys.stderr.write(message + "\n")
            return EXIT_TEST_WRITE_REFUSED
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
