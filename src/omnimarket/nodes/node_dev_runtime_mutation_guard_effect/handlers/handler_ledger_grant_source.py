# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Reads live grants from the tail of the rolling work ledger (OMN-21795).

A grant is one ledger row whose kind is one of the policy's ``row_kinds`` (an
OPERATOR-CONSENT row today, plus a GRANT row once the ledger grammar adds that kind).
The row's ``grant`` cell names ``compose:<project>`` and its ``until`` cell holds an
ISO-8601 time. The grant is live while ``until`` is in the future. It counts only
when ``until`` falls at most ``max_grant_hours`` after the row's own timestamp, so a
grant that never ends is ignored. A row may name several projects, one ``grant=``
cell each.

The rows come from the ledger reader node the policy's ``reader`` names, over the last
``max_grant_hours`` (rounded up) hours, for the policy's ``row_kinds`` only. No ledger
file is opened. A ``LedgerReadError`` or no matching row gives no grant, so the guard
refuses: an unreadable ledger never grants anything.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

from omnimarket.handlers.adapter_work_ledger_rows_reader import (
    SubprocessLedgerRowsReader,
)
from omnimarket.nodes.node_dev_runtime_mutation_guard_effect.models import (
    ModelDevRuntimeGrantPolicy,
)
from omnimarket.protocols.protocol_ledger_rows_reader import (
    LedgerReadError,
    ProtocolLedgerRowsReader,
)

_ROW_HEAD = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z) \| (?P<kind>[A-Z][A-Z-]*) \|"
)


def _cell(name: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![\w-]){re.escape(name)}=([^\s|;,\"']+)")


def _when(text: str) -> datetime | None:
    try:
        value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if value.tzinfo is not None else None


def grants_in(
    text: str, policy: ModelDevRuntimeGrantPolicy, now: datetime
) -> frozenset[str]:
    """The projects that live grant rows in ``text`` name at ``now``."""
    target = _cell(policy.target_cell)
    until = _cell(policy.until_cell)
    kinds = set(policy.row_kinds)
    limit = timedelta(hours=policy.max_grant_hours)
    live: set[str] = set()
    for line in text.splitlines():
        head = _ROW_HEAD.match(line)
        if head is None or head.group("kind") not in kinds:
            continue
        written = _when(head.group("ts"))
        ends = until.search(line)
        expiry = _when(ends.group(1)) if ends else None
        if written is None or expiry is None:
            continue
        if not (now < expiry and written <= expiry <= written + limit):
            continue
        for value in target.findall(line):
            if value.startswith(policy.target_prefix):
                live.add(value[len(policy.target_prefix) :])
    return frozenset(live)


class LedgerGrantSource:
    """Live grants from the rolling work ledger's rows, read through the ledger reader node."""

    def __init__(
        self,
        reader: ProtocolLedgerRowsReader | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._reader: ProtocolLedgerRowsReader = reader or SubprocessLedgerRowsReader(
            dict(os.environ if environ is None else environ)
        )
        self._error = ""

    def live_grants(
        self, policy: ModelDevRuntimeGrantPolicy, now: datetime
    ) -> frozenset[str]:
        try:
            lines = self._reader.rows(
                reader=policy.reader,
                window_hours=policy.window_hours,
                kinds=policy.row_kinds,
            )
        except LedgerReadError as exc:
            self._error = str(exc)
            return frozenset()
        self._error = ""
        return grants_in("\n".join(lines), policy, now)

    def handle(self, policy: ModelDevRuntimeGrantPolicy) -> frozenset[str]:
        """Dispatch entrypoint: the projects live grant rows name now."""
        return self.live_grants(policy, datetime.now(UTC))

    def describe(self, policy: ModelDevRuntimeGrantPolicy) -> str:
        if self._error:
            return f"the ledger rows of reader {policy.reader}, which could not be read ({self._error})"
        return f"the ledger rows of reader {policy.reader} (bus projection, last {policy.window_hours}h)"


__all__ = ["LedgerGrantSource", "grants_in"]
