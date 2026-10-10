# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The check names whose newest copy on a head is completed and cancelled (pure, definition-B)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.model_cancelled_checks_facts import (
    ModelCancelledChecksFacts,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.model_cancelled_checks_verdict import (
    ModelCancelledChecksVerdict,
)


def cancelled_of(ci: Mapping[str, Any], red: Iterable[str] = ()) -> tuple[str, ...]:
    """The names whose newest check copy on the head is completed and cancelled, sorted (OMN-20508).

    ``ci`` is the watcher record's ``ci``. Its ``cancelled`` list is used when the key is present: the watcher
    already left out a name with a queued or running copy or a newer success. A record written before that has no
    key, so the names come from ``runs`` (rows ``[name, status, conclusion, completed_at]``, several per name when
    older copies exist): a name with any row not ``completed`` has a successor and is left out; a name with one row
    is cancelled when that row is; a name with several rows needs every ``completed_at`` and is cancelled when every
    row at the newest stamp is. Rows carry no id, so a same-second pair with a non-cancelled copy leaves the name out
    (fail closed: the decision then refreshes nothing). A name in ``red`` is a failure, never cancelled."""
    if "cancelled" in ci:
        names = {str(n) for n in ci.get("cancelled") or ()}
    else:
        rows: dict[str, list[tuple[str, str, str]]] = {}
        for r in ci.get("runs") or ():
            if isinstance(r, (list, tuple)) and len(r) >= 3:
                rows.setdefault(str(r[0]), []).append(
                    (
                        str(r[1] or "").lower(),
                        str(r[2] or "").lower(),
                        str((r[3] if len(r) > 3 else "") or ""),
                    )
                )
        names = set()
        for name, copies in rows.items():
            if any(status != "completed" for status, _c, _t in copies):
                continue
            if len(copies) > 1:
                if not all(stamp for _s, _c, stamp in copies):
                    continue
                newest = max(stamp for _s, _c, stamp in copies)
                copies = [c for c in copies if c[2] == newest]
            if all(conclusion == "cancelled" for _s, conclusion, _t in copies):
                names.add(name)
    return tuple(sorted(names - {str(n) for n in red}))


class HandlerListCancelledChecks:
    """The cancelled check copies of one head (pure, definition-B)."""

    def handle(self, request: ModelCancelledChecksFacts) -> ModelCancelledChecksVerdict:
        return ModelCancelledChecksVerdict(
            cancelled=cancelled_of(
                request.ci.model_dump(exclude_unset=True), request.red
            )
        )


__all__: list[str] = ["HandlerListCancelledChecks", "cancelled_of"]
