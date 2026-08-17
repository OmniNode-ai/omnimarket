# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Author-time labels include ``ci:ready``, not just the marker (OMN-16071).

The OCC repo runs the OMN-15731 label-gated CI pilot: on a dev-targeting PR the
heavy ``pre-commit`` job runs ONLY when ``ci:ready`` is present, and CI Summary
then fails closed unless ``pre-commit`` actually SUCCEEDED — a skip blocks just
as hard as a failure. A machine-minted companion that is born without the label
is therefore not merely slow, it is **structurally unmergeable** until a human
hand-applies the label.

That is not hypothetical: OCC#6540 sat OPEN and BLOCKED carrying only
``occ:machine-minted``, and its peers #6533/#6529/#6515 merged only after the
label was hand-applied.

This is not a review gate being auto-satisfied. ``ci:ready`` decides whether the
wave RUNS; every gate still has to pass on its own merits, and CI Summary's
strict fail-closed block is untouched. Applying it at author time only stops the
writer from minting PRs it has made impossible to land.

Both minting call sites are covered — the RSD-3 write-EFFECT
(``node_occ_companion_effect``) and the legacy ``OccCompanionEmitter`` — because
either one alone leaves half the companions stalled.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest

from omnimarket.events.occ_autoauthor import (
    OCC_AUTHOR_TIME_LABELS,
    OCC_CI_READY_LABEL,
    OCC_MACHINE_MINTED_LABEL,
)
from omnimarket.nodes.node_occ_companion_effect.handlers.handler_occ_companion_effect import (
    HandlerOccCompanionEffect,
)


class _FakeResponse:
    def __init__(self, payload: object) -> None:
        self._raw = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._raw

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None


def _capture_label_post() -> tuple[dict[str, Any], Any]:
    captured: dict[str, Any] = {}

    def _fake_urlopen(req, timeout=None):  # type: ignore[no-untyped-def]
        captured["url"] = req.full_url
        captured["data"] = json.loads(req.data) if req.data else None
        return _FakeResponse([{"id": 1, "name": OCC_MACHINE_MINTED_LABEL}])

    return captured, _fake_urlopen


@pytest.mark.unit
class TestAuthorTimeLabels:
    def test_the_constant_pairs_the_marker_with_ci_ready(self) -> None:
        """The seam both writers share must carry exactly these two labels."""
        assert OCC_AUTHOR_TIME_LABELS == (
            OCC_MACHINE_MINTED_LABEL,
            OCC_CI_READY_LABEL,
        )
        assert OCC_CI_READY_LABEL == "ci:ready"

    def test_effect_handler_applies_ci_ready_with_the_marker(self) -> None:
        """RED before the fix: the POST body carried only the marker label.

        A companion born without ``ci:ready`` can never satisfy CI Summary on a
        dev-targeting PR, which is how OCC#6540 stalled.
        """
        handler = HandlerOccCompanionEffect.__new__(HandlerOccCompanionEffect)
        captured, fake_urlopen = _capture_label_post()

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            handler._apply_machine_minted_label(
                "OmniNode-ai", "onex_change_control", 6540, "tok"
            )

        assert captured["data"] == {
            "labels": [OCC_MACHINE_MINTED_LABEL, OCC_CI_READY_LABEL]
        }
        assert captured["url"].endswith("/issues/6540/labels")

    def test_both_labels_land_in_a_single_post(self) -> None:
        """One call, not two — a second POST could partially fail and re-stall."""
        handler = HandlerOccCompanionEffect.__new__(HandlerOccCompanionEffect)
        calls: list[dict[str, Any]] = []

        def _fake_urlopen(req, timeout=None):  # type: ignore[no-untyped-def]
            calls.append(json.loads(req.data) if req.data else {})
            return _FakeResponse([{"id": 1, "name": OCC_MACHINE_MINTED_LABEL}])

        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            handler._apply_machine_minted_label(
                "OmniNode-ai", "onex_change_control", 6540, "tok"
            )

        assert len(calls) == 1
        assert set(calls[0]["labels"]) == set(OCC_AUTHOR_TIME_LABELS)

    def test_legacy_emitter_uses_the_same_author_time_labels(self) -> None:
        """The legacy emitter mints on the same branch prefix and must match.

        If only the EFFECT path is fixed, every companion minted by the emitter
        keeps stalling, and the two writers become silently divergent.
        """
        source = (
            "src/omnimarket/nodes/node_pr_lifecycle_fix_effect/handlers/"
            "occ_companion_emitter.py"
        )
        from pathlib import Path

        repo_root = Path(__file__).resolve().parents[4]
        text = (repo_root / source).read_text()
        assert 'body={"labels": list(OCC_AUTHOR_TIME_LABELS)}' in text, (
            "the legacy emitter must post the shared author-time label tuple, "
            "not a bare marker list"
        )
