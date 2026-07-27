# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-13965: skill-mapping input-coverage gate as a CI test.

Proves the invariant that broke ``create_ticket`` in OMN-13964: every
``skill_mapping.yaml`` arg carrying a ``default`` (force-injected into the
node-input payload on every ``onex skill`` call) must be accepted by the model
the backing handler validates. A defaulted arg the model rejects (``extra=
"forbid"`` + missing field) fails 100% of that skill's CLI invocations with
``extra_forbidden``. The validator resolves, per skill, the runtime-validated
input model(s) and flags any defaulted arg no candidate accepts. See
``scripts/validate_skill_mapping_input_coverage.py`` for the resolution rules.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "validate_skill_mapping_input_coverage.py"
)


def _load_validator() -> object:
    spec = importlib.util.spec_from_file_location(
        "validate_skill_mapping_input_coverage", _SCRIPT
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.integration
def test_no_defaulted_skill_arg_is_rejected_by_its_node_input_model() -> None:
    """Regression gate for OMN-13964: no skill has a rejected defaulted arg."""
    report = _load_validator().evaluate()  # type: ignore[attr-defined]

    fails = [r for r in report["results"] if r["status"] == "FAIL"]
    detail = "\n".join(
        f"  {r['skill']} ({r['node']}): defaulted args not accepted by "
        f"{r.get('candidate_models')}: {r['missing_fields']}"
        for r in fails
    )
    assert not fails, (
        "skill_mapping.yaml defaulted args must be accepted by the backing node's "
        "input model (OMN-13964 class of 100%-broken skills):\n" + detail
    )


@pytest.mark.integration
def test_gate_actually_resolved_skills() -> None:
    """Guard against a silent no-op: the gate must have checked real skills."""
    report = _load_validator().evaluate()  # type: ignore[attr-defined]
    assert int(report["checked"]) > 0, "skill registry resolved zero skills"
    # create_ticket must resolve to a concrete input model (not silently skipped),
    # otherwise the OMN-13964 regression would slip through as a SKIP.
    create_ticket = next(
        (r for r in report["results"] if r["skill"] == "create_ticket"), None
    )
    assert create_ticket is not None, "create_ticket skill missing from registry"
    assert create_ticket["status"] != "SKIP", (
        f"create_ticket input model was not resolvable: {create_ticket.get('detail')}"
    )
