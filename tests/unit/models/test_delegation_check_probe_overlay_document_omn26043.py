# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The delegation check probe's overlay document is a shared model the overlays repository can pin (OMN-26043).

The overlays repository pins a node's overlay to a merged revision that already holds the model, so the model lives
in the shared overlay documents package and merges ahead of the node that reads it.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnimarket.models.overlay_documents.model_delegation_check_probe_overlay import (
    ModelDelegationCheckProbeOverlay,
)

_DOC = {
    "checks": ["C13", "C14", "C17"],
    "history_file": "delegation-check-runs.jsonl",
    "history_keep": 120,
    "result_topic": "onex.evt.omnimarket.delegation-check-probe-completed.v1",
    "responder_timeout_s": 10,
}


def test_the_complete_document_loads() -> None:
    doc = ModelDelegationCheckProbeOverlay.model_validate(_DOC)

    assert doc.checks == ("C13", "C14", "C17")
    assert doc.history_keep == 120
    assert doc.responder_timeout_s == 10


@pytest.mark.parametrize("key", sorted(_DOC))
def test_a_document_missing_a_key_is_refused(key: str) -> None:
    broken = {k: v for k, v in _DOC.items() if k != key}

    with pytest.raises(ValidationError):
        ModelDelegationCheckProbeOverlay.model_validate(broken)


def test_an_unknown_key_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelDelegationCheckProbeOverlay.model_validate({**_DOC, "gateway_url": "x"})


def test_a_check_this_node_does_not_run_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelDelegationCheckProbeOverlay.model_validate({**_DOC, "checks": ["C99"]})
