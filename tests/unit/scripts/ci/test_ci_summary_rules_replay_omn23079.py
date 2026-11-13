# SPDX-License-Identifier: MIT
"""OMN-23079: CI Summary's verdict on recorded heads is unchanged by the rules port.

The job and context tables moved from ``scripts/ci/ci_summary_gate.py`` into the
overlay ``scripts/ci/ci_summary_rules.json``. The fixture holds pull request heads
recorded by the PR watcher mirror (check-run name, status, conclusion, completed_at,
id) and the verdict the pre-port gate gave each one under three events, recorded
at a fixed observation time. The replay feeds the same rows to the ported gate and
asserts the exit code of each layer and the exact report bytes (by sha256) match.

The control is a named injected break: drop one strict gate job from a copy of the
overlay and the same replay must read different, or the replay proves nothing.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
GATE = REPO_ROOT / "scripts" / "ci" / "ci_summary_gate.py"
RULES = GATE.with_name("ci_summary_rules.json")
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "ci_summary" / "replay_heads_omn23079.json"
SCENARIOS: tuple[tuple[str, str | None], ...] = (
    ("pull_request", None),
    ("merge_group", None),
    ("push", "dev"),
)
INJECTED_BREAK_JOB = "Repo Evidence Dependency"


def _fixture() -> dict[str, object]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _now() -> datetime:
    return datetime.strptime(str(_fixture()["now"]), "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=UTC
    )


def _load_gate(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(name, None)
    return module


def _replay(
    gate: ModuleType, rows: list[dict[str, object]], registry: Path
) -> dict[str, object]:
    # The gate reads the registry the node resolved (OMN-23505), not the files.
    gate.REGISTRY_FILE = registry
    now = _now()
    external = frozenset(gate.EXPECTED_EXTERNAL_CONTEXTS)
    jobs = [row for row in rows if row["name"] not in external]
    verdicts: dict[str, object] = {}
    for event, ref in SCENARIOS:
        code, report = gate.evaluate(jobs, now=now, event=event, ref_name=ref)
        expected = gate.expected_external_contexts(event, ref_name=ref)
        ext_code, ext_report = gate.evaluate_external(
            rows, expected=expected, now=now, head_workflow_runs=[]
        )
        sweep_code, sweep_report = gate.evaluate_external_sweep(
            rows,
            expected=expected,
            in_run_names=frozenset(gate.dedup_latest(jobs)),
            own_run_ids=frozenset(),
            workflow_runs=[],
            now=now,
            event=event,
        )
        text = "\n".join([report, ext_report, sweep_report])
        verdicts[f"{event}:{ref or ''}"] = {
            "exit": gate._worse(gate._worse(code, ext_code), sweep_code),
            "layers": [code, ext_code, sweep_code],
            "report_sha256": hashlib.sha256(text.encode()).hexdigest(),
        }
    return verdicts


def _heads() -> list[dict[str, object]]:
    heads = _fixture()["heads"]
    assert isinstance(heads, list)
    return heads


def test_fixture_covers_pass_fail_and_pending_under_every_event() -> None:
    seen = {
        (key, verdict["exit"])
        for head in _heads()
        for key, verdict in head["verdicts"].items()
    }
    for key in ("pull_request:", "merge_group:"):
        assert {(key, 0), (key, 1), (key, 2)} <= seen
    assert {("push:dev", 1), ("push:dev", 2)} <= seen


@pytest.mark.parametrize("head", _heads(), ids=lambda h: f"pr{h['pr']}-{h['sha'][:8]}")
def test_ported_gate_replays_the_recorded_verdict(
    head: dict[str, object], resolved_registry: Path
) -> None:
    gate = _load_gate(GATE, "ci_summary_gate_replay_omn23079")
    assert _replay(gate, head["check_runs"], resolved_registry) == head["verdicts"]


def test_injected_break_in_the_overlay_changes_a_replayed_verdict(
    tmp_path: Path, resolved_registry: Path
) -> None:
    """Control: the replay reads the overlay, so a dropped strict job must show."""
    for sibling in GATE.parent.glob("ci_summary_*"):
        shutil.copy(sibling, tmp_path / sibling.name)
    doc = json.loads(RULES.read_text(encoding="utf-8"))
    entries = doc["rules"]["STRICT_GATE_JOBS"]["entries"]
    kept = [e for e in entries if e["name"] != INJECTED_BREAK_JOB]
    assert len(kept) == len(entries) - 1
    doc["rules"]["STRICT_GATE_JOBS"]["entries"] = kept
    (tmp_path / RULES.name).write_text(json.dumps(doc), encoding="utf-8")
    broken = _load_gate(tmp_path / GATE.name, "ci_summary_gate_broken_omn23079")
    assert INJECTED_BREAK_JOB not in broken.STRICT_GATE_JOBS
    changed = [
        head["pr"]
        for head in _heads()
        if _replay(broken, head["check_runs"], resolved_registry) != head["verdicts"]
    ]
    assert changed, "dropping a strict gate job left every replayed verdict unchanged"


@pytest.mark.parametrize(
    "mutation",
    ["missing", "unparsable", "wrong-contract", "wrong-kind", "repeated-entry"],
)
def test_malformed_overlay_refuses_at_import(tmp_path: Path, mutation: str) -> None:
    for sibling in GATE.parent.glob("ci_summary_*"):
        shutil.copy(sibling, tmp_path / sibling.name)
    target = tmp_path / RULES.name
    doc = json.loads(RULES.read_text(encoding="utf-8"))
    if mutation == "missing":
        target.unlink()
    elif mutation == "unparsable":
        target.write_text("{", encoding="utf-8")
    else:
        if mutation == "wrong-contract":
            doc["contract"] = "something_else"
        elif mutation == "wrong-kind":
            doc["rules"]["SOFT_ALLOWLIST"]["kind"] = "tuple"
        else:
            entries = doc["rules"]["STRICT_GATE_JOBS"]["entries"]
            entries.append(dict(entries[0]))
        target.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises((OSError, ValueError, KeyError)):
        _load_gate(tmp_path / GATE.name, f"ci_summary_gate_malformed_{mutation}")


def _static_expected_external_contexts(path: Path) -> list[str]:
    """Read the constant the way omniclaude's advisory-job gate does: statically."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "EXPECTED_EXTERNAL_CONTEXTS"
            and node.value is not None
        ):
            return [str(item) for item in ast.literal_eval(node.value)]
    raise AssertionError("EXPECTED_EXTERNAL_CONTEXTS absent from the gate file")


def test_expected_external_contexts_stays_a_static_literal() -> None:
    """The cross-repo advisory-job gate parses this constant with ast.literal_eval.

    Reading it through the overlay made that gate fail with "is not a literal" on
    the first head of this change. The named injected break is the call form the
    first head used.
    """
    gate = _load_gate(GATE, "ci_summary_gate_static_literal")
    assert _static_expected_external_contexts(GATE) == list(
        gate.EXPECTED_EXTERNAL_CONTEXTS
    )
    call_form = ast.parse('RULES.tuple("EXPECTED_EXTERNAL_CONTEXTS")', mode="eval")
    with pytest.raises(ValueError, match="malformed node"):
        ast.literal_eval(call_form.body)
