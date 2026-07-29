# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""``state-coverage-gate`` must not treat a node's DDL as a node change.

## Why this exists

``scripts/validate_state_coverage.py --check-changed <ref> --strict`` promotes a
node's *baselined* state-coverage debt from WARN to FAIL when that node is
"directly modified". Direct modification was any path under
``src/omnimarket/nodes/<node>/`` — which includes ``migrations/*.sql``.

A vendored migration is not the node. Declared FSM states and declared output
topics come from ``contract.yaml`` alone, so a DDL edit cannot add, remove or
rename one; un-grandfathering a node's unrelated pre-existing debt because its
SQL moved is the same false coupling OMN-14009 already fixed once for the
``contract_touched`` shape comparison.

Observed cost: the OMN-15376 shape-drift reconciliation edits
``migrations/*.sql`` under 46 node directories and nothing else. Before this
scoping, that turned **16** unrelated nodes red on a REQUIRED dev context
(``state-coverage-gate``) — ``node_canary_score_reducer``,
``node_projection_traces``, ``node_projection_voice_sessions`` and 13 more —
none of which had a state or topic changed by that PR.

## What is NOT relaxed

A change to ``contract.yaml`` or to any ``.py`` under the node, and a change to
the node's own tests, still flag it. A node whose migration AND whose
``contract.yaml``/handler moved in the same PR is still flagged, because the
non-migration file is what flags it.

Ticket: OMN-15376
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "validate_state_coverage.py"


def _load_module() -> object:
    import importlib.util
    import sys

    name = "omn15376_validate_state_coverage"
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered before exec: @dataclass resolves its own module out of
    # sys.modules, and a file-loaded script that is absent from it raises
    # AttributeError: 'NoneType' object has no attribute '__dict__'.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _changed_nodes(monkeypatch: pytest.MonkeyPatch, files: list[str]) -> set[str]:
    module = _load_module()

    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=["git"], returncode=0, stdout="\n".join(files) + "\n", stderr=""
        )

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    _nodes, directly_modified, _contract_touched = module._get_changed_nodes("HEAD")
    return directly_modified


def test_migration_only_change_does_not_flag_the_node(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact OMN-15376 diff shape: SQL under migrations/, nothing else."""
    flagged = _changed_nodes(
        monkeypatch,
        [
            "src/omnimarket/nodes/node_projection_traces/migrations/"
            "0001_create_traces.sql",
            "src/omnimarket/nodes/node_canary_score_reducer/migrations/"
            "0001_create_capability_scores.sql",
        ],
    )
    assert flagged == set(), flagged


def test_contract_change_still_flags_the_node(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Anti-over-correction: the gate still fires on the artifact that matters."""
    flagged = _changed_nodes(
        monkeypatch, ["src/omnimarket/nodes/node_projection_traces/contract.yaml"]
    )
    assert flagged == {"node_projection_traces"}, flagged


def test_python_change_still_flags_the_node(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A handler edit is a node edit and is unaffected by this scoping."""
    flagged = _changed_nodes(
        monkeypatch,
        [
            "src/omnimarket/nodes/node_projection_traces/handlers/handler_traces.py",
        ],
    )
    assert flagged == {"node_projection_traces"}, flagged


def test_mixed_change_still_flags_the_node(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A migration alongside a real node change does not launder the node."""
    flagged = _changed_nodes(
        monkeypatch,
        [
            "src/omnimarket/nodes/node_projection_traces/migrations/"
            "0001_create_traces.sql",
            "src/omnimarket/nodes/node_projection_traces/node.py",
        ],
    )
    assert flagged == {"node_projection_traces"}, flagged


# ---------------------------------------------------------------------------
# golden-chain-coverage-gate: the same scoping, for the same reason.
# ---------------------------------------------------------------------------
GOLDEN_CHAIN_SCRIPT = REPO_ROOT / "scripts" / "ci" / "check_golden_chain_coverage.py"


def _golden_chain_nodes(files: list[str]) -> set[str]:
    import importlib.util
    import sys

    name = "omn15376_check_golden_chain_coverage"
    spec = importlib.util.spec_from_file_location(name, GOLDEN_CHAIN_SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module._node_names_from_changed_files(files)


def test_golden_chain_gate_ignores_migration_only_changes() -> None:
    """A DDL edit cannot change whether a golden-chain test file exists."""
    assert (
        _golden_chain_nodes(
            [
                "src/omnimarket/nodes/node_projection_live_events/migrations/"
                "0000_create_live_events.sql"
            ]
        )
        == set()
    )


def test_golden_chain_gate_still_fires_on_a_handler_change() -> None:
    """Anti-over-correction: real live-path edits still demand a golden chain."""
    assert _golden_chain_nodes(
        [
            "src/omnimarket/nodes/node_projection_live_events/handlers/"
            "handler_live_events.py"
        ]
    ) == {"node_projection_live_events"}
