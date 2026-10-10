# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerCaptureRows: the ledger rows that record one classified operator message (OMN-20905).

Pure and deterministic: the rows are a function of the message, its classification, the drift
of each decision item and the stamp handed in. One STATUS row per non-chat item, under the lane
``operator-capture``, carrying the item's kind, a stable item id, the session id, where the
message came from, who classified it, and the operator's words verbatim in double quotes as the
row's free text. An ask also carries ``ask=<id>`` and ``state=open``: it stays open until a later
row cites ``closes-ask=<id>`` with evidence, or the operator drops it.

A ledger row is one line split on `` | ``, so three characters of the words cannot be kept
byte for byte: a line break becomes `` / ``, a pipe becomes a broken bar and a double quote
becomes two single quotes. A row that needed any of these says ``verbatim=normalized``; the
untouched text stays in the local capture store under ``capture=<id>``.
"""

from __future__ import annotations

import re

from omnimarket.models.operator_capture import (
    EnumClassifierSource,
    EnumPromptOrigin,
    EnumUtteranceKind,
    ModelCaptureRows,
    ModelCaptureRowsRequest,
)

STAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_CELL_UNSAFE = re.compile(r"[|\n\r=]")


def ledger_words(text: str) -> tuple[str, bool]:
    """The words as one ledger cell, and whether anything had to change."""
    out = re.sub(r"\s*\r?\n\s*", " / ", text.strip())
    out = out.replace("|", "¦").replace('"', "''")
    return out, out != text.strip()


def _cell(value: str) -> str:
    return " ".join(_CELL_UNSAFE.sub(" ", value).split())


class HandlerCaptureRows:
    """Build the capture rows; a machine injection or an all-chat message yields none."""

    def handle(self, request: ModelCaptureRowsRequest) -> ModelCaptureRows:
        classification = request.classification
        if classification.origin is EnumPromptOrigin.MACHINE:
            return ModelCaptureRows()
        utterance = request.utterance
        stamp = request.stamp.strftime(STAMP_FORMAT)
        if classification.classifier is EnumClassifierSource.DELEGATED:
            classifier = (
                f"delegated:{_cell(classification.classifier_model or 'unattributed')}"
            )
        else:
            classifier = "heuristic"
        common = [
            f"session={_cell(utterance.session_id)}",
            f"source={_cell(utterance.source)}",
            f"capture={utterance.capture_id}",
        ]
        rows: list[str] = []
        for item in classification.items:
            if item.kind is EnumUtteranceKind.CHAT:
                continue
            cells = [stamp, "STATUS", f"lane={request.lane}", f"kind={item.kind.value}"]
            cells.append(f"item={item.item_id}")
            if item.kind is EnumUtteranceKind.ASK:
                cells += [f"ask=ask-{item.item_id.removeprefix('cap-')}", "state=open"]
            cells += common
            cells += [f"classifier={classifier}", f"confidence={item.confidence:.2f}"]
            if item.subject:
                cells.append(f"subject={_cell(item.subject)}")
            drift = request.drift.get(item.item_id)
            if drift is not None and drift.matches:
                relation = "contradicts" if drift.contradicts else "reaffirms"
                prior = ",".join(m.stamp for m in drift.matches)
                cells += ["drift=re-ruled", f"relation={relation}", f"prior={prior}"]
            words, changed = ledger_words(item.quote)
            if len(words) > request.max_quote_chars:
                words = words[: request.max_quote_chars].rstrip() + " ..."
                cells.append("truncated=yes")
            if changed:
                cells.append("verbatim=normalized")
            cells.append(f'"{words}"')
            rows.append(" | ".join(cells))
        for ask in classification.drops:
            words, changed = ledger_words(utterance.text)
            cells = [
                stamp,
                "STATUS",
                f"lane={request.lane}",
                f"closes-ask={ask}",
                "state=dropped",
                "evidence=operator-drop",
                *common,
            ]
            if changed:
                cells.append("verbatim=normalized")
            cells.append(f'"{words[: request.max_quote_chars]}"')
            rows.append(" | ".join(cells))
        return ModelCaptureRows(rows=tuple(rows))


__all__ = ["STAMP_FORMAT", "HandlerCaptureRows", "ledger_words"]
