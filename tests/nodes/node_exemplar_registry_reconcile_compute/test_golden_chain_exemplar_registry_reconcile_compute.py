# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21128: TC.5 exemplar registry reconciliation through the contract and bus.

An admission result at a commit is reconciled against the registry's current rows
for that repository into one plan row per logical source: create, revise, retain,
deprecate or revoke. Removal never drops a source silently. Golden chain dispatches
the request through RuntimeLocal; error chain refuses a repository mismatch.
"""

from __future__ import annotations

import hashlib
import importlib
import json
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.enums.enum_workflow_result import EnumWorkflowResult
from pydantic import ValidationError

import omnimarket.nodes.node_exemplar_registry_reconcile_compute as node_package
from omnimarket.nodes.node_exemplar_registry_reconcile_compute.handlers.handler_exemplar_registry_reconcile import (
    HandlerExemplarRegistryReconcile,
)
from omnimarket.nodes.node_exemplar_registry_reconcile_compute.models.model_exemplar_registry_reconcile import (
    ModelExemplarRegistryReconcileRequest,
    ModelExemplarRegistryReconcileResult,
    ModelExemplarRegistryRow,
)
from tests.runtime_local_compat import RuntimeLocal

pytestmark = pytest.mark.unit

NODE_DIR = Path(node_package.__file__).parent
COMMAND_TOPIC = "onex.cmd.omnimarket.exemplar-registry-reconcile-requested.v1"
TERMINAL_TOPIC = "onex.evt.omnimarket.exemplar-registry-reconciled.v1"
REPO = "omnimarket"
COMMIT = "b" * 40
GATE_VERSION = "canonical-handler-shape.v3"
RULES_VERSION = "exemplar-admission-rules.v1"
HANDLER = HandlerExemplarRegistryReconcile()
OPERATIONS = ("create", "revise", "retain", "deprecate", "revoke")


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def decision(
    node_id: str, *, admitted: bool = True, codes: tuple[str, ...] = ()
) -> dict[str, Any]:
    return {
        "node_id": node_id,
        "node_archetype": "compute",
        "contract_path": f"src/omnimarket/nodes/{node_id}/contract.yaml",
        "handler_module": f"omnimarket.nodes.{node_id}.handlers.handler",
        "admitted": admitted,
        "violations": [{"code": code, "detail": ""} for code in codes],
        "coverage_state": "unknown",
        "coverage_percent": None,
        "handler_lines": 10,
        "rank": 1 if admitted else None,
        "receipt": (
            {
                "gate": "canonical-handler-shape",
                "gate_version": GATE_VERSION,
                "baseline_sha256": "c" * 64,
                "result": "pass",
                "commit": COMMIT,
                "check_run_ids": [7],
            }
            if admitted
            else None
        ),
    }


def admission(*decisions: dict[str, Any], repository: str = REPO) -> dict[str, Any]:
    admitted = [d["node_id"] for d in decisions if d["admitted"]]
    return {
        "repository": repository,
        "commit": COMMIT,
        "gate": "canonical-handler-shape",
        "gate_version": GATE_VERSION,
        "rules_version": RULES_VERSION,
        "pure_import_list_version": "pure-import-deny.v1",
        "decisions": list(decisions),
        "admitted_by_archetype": {"compute": admitted} if admitted else {},
        "admitted_count": len(admitted),
        "refused_count": len(decisions) - len(admitted),
    }


def request(
    *decisions: dict[str, Any],
    rows: list[dict[str, Any]] | None = None,
    digests: dict[str, str] | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    if digests is None:
        digests = {d["node_id"]: sha(d["node_id"]) for d in decisions if d["admitted"]}
    payload: dict[str, Any] = {
        "repository": REPO,
        "admission": admission(*decisions),
        "content_sha256": digests,
        "current_rows": rows or [],
    }
    payload.update(overrides)
    return payload


def reconcile(payload: dict[str, Any]) -> ModelExemplarRegistryReconcileResult:
    return HANDLER.handle(ModelExemplarRegistryReconcileRequest.model_validate(payload))


def ops(result: ModelExemplarRegistryReconcileResult) -> dict[str, str]:
    return {op.node_id: op.operation for op in result.operations}


def current_row(
    node_id: str, lifecycle: str = "active", **overrides: Any
) -> dict[str, Any]:
    """The row a create of ``node_id`` at the default digest would leave behind."""
    created = reconcile(request(decision(node_id))).operations[0]
    row = {
        "logical_source_id": created.logical_source_id,
        "node_id": node_id,
        "version_digest": created.version_digest,
        "lifecycle": lifecycle,
        "commit": "a" * 40,
    }
    row.update(overrides)
    return row


def _contract() -> dict[str, Any]:
    return dict(yaml.safe_load((NODE_DIR / "contract.yaml").read_text()))


def test_contract_declares_topics_models_handler_and_entry_point() -> None:
    contract = _contract()
    assert contract["node_type"] == "compute"
    assert contract["descriptor"]["purity"] == "pure"
    assert contract["runtime_dispatch"]["command_topic"] == COMMAND_TOPIC
    assert contract["event_bus"]["subscribe_topics"] == [COMMAND_TOPIC]
    assert contract["event_bus"]["publish_topics"] == [TERMINAL_TOPIC]
    assert contract["terminal_event"] == TERMINAL_TOPIC
    assert (
        contract["handler_routing"]["handlers"][0]["operation"]
        == "reconcile_exemplar_registry"
    )
    binding = contract["handler"]
    handler_type = getattr(importlib.import_module(binding["module"]), binding["class"])
    assert issubclass(node_package.NodeExemplarRegistryReconcileCompute, handler_type)
    for side, model in (
        ("input_model", ModelExemplarRegistryReconcileRequest),
        ("output_model", ModelExemplarRegistryReconcileResult),
    ):
        declared = getattr(
            importlib.import_module(contract[side]["module"]), contract[side]["name"]
        )
        assert declared is model
        assert binding[side] == f"{model.__module__}.{model.__name__}"
    registered = {e.name: e for e in entry_points(group="onex.nodes")}
    assert registered["node_exemplar_registry_reconcile_compute"].load() is node_package


def test_new_admitted_source_is_created_active_with_its_receipt() -> None:
    result = reconcile(request(decision("node_a")))
    [op] = result.operations
    assert op.operation == "create"
    assert op.logical_source_id == f"{REPO}/node_a"
    assert op.lifecycle_after == "active"
    assert op.prior_version_digest is None
    assert op.version_digest is not None
    assert len(op.version_digest) == 64
    assert op.receipt is not None
    assert op.receipt.check_run_ids == [7]
    assert result.counts == {
        "create": 1,
        "revise": 0,
        "retain": 0,
        "deprecate": 0,
        "revoke": 0,
    }


def test_refused_source_absent_from_registry_gets_no_plan_row() -> None:
    result = reconcile(
        request(decision("node_x", admitted=False, codes=("in_baseline",)))
    )
    assert result.operations == []
    assert result.counts == dict.fromkeys(OPERATIONS, 0)


def test_unchanged_active_source_is_a_typed_retain_not_an_omission() -> None:
    row = current_row("node_a")
    [op] = reconcile(request(decision("node_a"), rows=[row])).operations
    assert op.operation == "retain"
    assert op.lifecycle_after == "active"
    assert op.prior_version_digest == op.version_digest == row["version_digest"]


def test_changed_content_revises_with_new_version_digest() -> None:
    row = current_row("node_a")
    payload = request(
        decision("node_a"), rows=[row], digests={"node_a": sha("changed")}
    )
    [op] = reconcile(payload).operations
    assert op.operation == "revise"
    assert op.prior_version_digest == row["version_digest"]
    assert op.version_digest not in (None, row["version_digest"])


def test_gate_version_bump_revises() -> None:
    row = current_row("node_a")
    payload = request(decision("node_a"), rows=[row])
    payload["admission"]["gate_version"] = "canonical-handler-shape.v4"
    assert ops(reconcile(payload)) == {"node_a": "revise"}


def test_active_source_missing_from_admission_is_deprecated_never_dropped() -> None:
    row = current_row("node_gone")
    [op] = reconcile(request(rows=[row])).operations
    assert op.operation == "deprecate"
    assert op.lifecycle_after == "deprecated"
    assert op.reason_codes == ["absent_at_commit"]
    assert op.prior_version_digest == row["version_digest"]
    assert op.receipt is None


def test_active_source_refused_for_ordinary_reason_is_deprecated_with_codes() -> None:
    row = current_row("node_a")
    refused = decision("node_a", admitted=False, codes=("in_baseline", "handle_kwargs"))
    [op] = reconcile(request(refused, rows=[row])).operations
    assert (op.operation, op.lifecycle_after) == ("deprecate", "deprecated")
    assert op.reason_codes == ["in_baseline", "handle_kwargs"]


@pytest.mark.parametrize(
    "code", ["gate_failed", "deny_secret_literal", "deny_eval_exec", "deny_pickle"]
)
def test_refusal_with_revoke_code_revokes(code: str) -> None:
    for lifecycle in ("active", "deprecated"):
        row = current_row("node_a", lifecycle=lifecycle)
        refused = decision("node_a", admitted=False, codes=("in_baseline", code))
        [op] = reconcile(
            request(
                refused, rows=[row], evaluation_counts={"node_a": {"true_positive": 3}}
            )
        ).operations
        assert (op.operation, op.lifecycle_after) == ("revoke", "revoked")
        assert op.reason_codes == ["in_baseline", code]


def test_revoke_codes_are_declared_policy_data() -> None:
    row = current_row("node_a")
    refused = decision("node_a", admitted=False, codes=("in_baseline",))
    payload = request(refused, rows=[row])
    payload["evaluation_counts"] = {"node_a": {"true_positive": 3}}
    payload["policy"] = {"policy_version": "p.test", "revoke_on_codes": ["in_baseline"]}
    result = reconcile(payload)
    assert ops(result) == {"node_a": "revoke"}
    assert result.policy_version == "p.test"


def test_deprecated_source_still_absent_is_retained_deprecated() -> None:
    row = current_row("node_a", lifecycle="deprecated")
    [op] = reconcile(request(rows=[row])).operations
    assert (op.operation, op.lifecycle_after) == ("retain", "deprecated")


def test_deprecated_source_admitted_again_is_revised_active() -> None:
    row = current_row("node_a", lifecycle="deprecated")
    [op] = reconcile(request(decision("node_a"), rows=[row])).operations
    assert (op.operation, op.lifecycle_after) == ("revise", "active")


def test_revoked_source_with_same_bytes_stays_revoked() -> None:
    row = current_row("node_a", lifecycle="revoked")
    [op] = reconcile(request(decision("node_a"), rows=[row])).operations
    assert (op.operation, op.lifecycle_after) == ("retain", "revoked")
    assert op.receipt is None


def test_revoked_source_with_new_bytes_is_revised_active() -> None:
    row = current_row("node_a", lifecycle="revoked")
    payload = request(decision("node_a"), rows=[row], digests={"node_a": sha("fixed")})
    [op] = reconcile(payload).operations
    assert (op.operation, op.lifecycle_after) == ("revise", "active")


def test_revoked_source_absent_or_refused_is_retained_revoked() -> None:
    row = current_row("node_a", lifecycle="revoked")
    refused = decision("node_a", admitted=False, codes=("deny_pickle",))
    for payload in (request(rows=[row]), request(refused, rows=[row])):
        [op] = reconcile(payload).operations
        assert (op.operation, op.lifecycle_after) == ("retain", "revoked")


def test_plan_is_ordered_by_logical_source_and_counts_bind_it() -> None:
    rows = [current_row("node_c"), current_row("node_b"), current_row("node_e")]
    payload = request(
        decision("node_d"),
        decision("node_b"),
        decision("node_e", admitted=False, codes=("deny_pickle",)),
        decision("node_a"),
        rows=rows,
        evaluation_counts={"node_e": {"true_positive": 3}},
    )
    result = reconcile(payload)
    assert [op.node_id for op in result.operations] == [
        "node_a",
        "node_b",
        "node_c",
        "node_d",
        "node_e",
    ]
    assert ops(result) == {
        "node_a": "create",
        "node_b": "retain",
        "node_c": "deprecate",
        "node_d": "create",
        "node_e": "revoke",
    }
    assert result.counts == {
        "create": 2,
        "revise": 0,
        "retain": 1,
        "deprecate": 1,
        "revoke": 1,
    }
    assert result.current_count == 3
    assert result.admitted_count == 3
    assert result.commit == COMMIT
    assert len({op.operation_id for op in result.operations}) == 5


def test_reconciliation_is_deterministic_and_input_order_independent() -> None:
    rows = [current_row("node_b"), current_row("node_c")]
    first = request(decision("node_a"), decision("node_b"), rows=rows)
    second = request(decision("node_b"), decision("node_a"), rows=list(reversed(rows)))
    one, two = reconcile(first), reconcile(second)
    assert one == two
    assert len(one.reconciliation_hash) == 64
    changed = request(
        decision("node_a"),
        decision("node_b"),
        rows=rows,
        digests={"node_a": sha("x"), "node_b": sha("node_b")},
    )
    assert reconcile(changed).reconciliation_hash != one.reconciliation_hash


def test_admitted_source_without_content_digest_fails_closed() -> None:
    with pytest.raises(ValueError, match="node_a"):
        reconcile(request(decision("node_a"), digests={}))


def test_malformed_content_digest_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelExemplarRegistryReconcileRequest.model_validate(
            request(decision("node_a"), digests={"node_a": "not-a-sha"})
        )


def test_row_from_another_repository_fails_closed() -> None:
    row = current_row("node_a", logical_source_id="omnibase_core/node_a")
    with pytest.raises(ValueError, match="omnibase_core/node_a"):
        reconcile(request(decision("node_a"), rows=[row]))


def test_duplicate_current_rows_fail_closed() -> None:
    row = current_row("node_a")
    with pytest.raises(ValueError, match="duplicate"):
        reconcile(request(decision("node_a"), rows=[row, row]))


def test_duplicate_admission_decisions_fail_closed() -> None:
    with pytest.raises(ValueError, match="duplicate admission decision: node_a"):
        reconcile(request(decision("node_a"), decision("node_a")))


def test_admission_for_another_repository_fails_closed() -> None:
    payload = request(decision("node_a"))
    payload["admission"]["repository"] = "omnibase_core"
    with pytest.raises(ValueError, match="omnibase_core"):
        reconcile(payload)


def test_row_lifecycle_and_digest_are_validated() -> None:
    for bad in ({"lifecycle": "deleted"}, {"version_digest": "short"}):
        with pytest.raises(ValidationError):
            ModelExemplarRegistryRow.model_validate(current_row("node_a", **bad))


async def _run(
    tmp_path: Path, payload: dict[str, Any], name: str = "state"
) -> RuntimeLocal:
    (tmp_path / name).mkdir(exist_ok=True)
    input_path = tmp_path / f"{name}.json"
    input_path.write_text(json.dumps(payload))
    runtime = RuntimeLocal(
        workflow_path=NODE_DIR / "contract.yaml",
        input_path=input_path,
        state_root=tmp_path / name,
        backend_overrides={"event_bus": "inmemory"},
        timeout=10,
    )
    await runtime.run_async()
    return runtime


@pytest.mark.asyncio
async def test_golden_chain_over_the_bus_plans_create_and_deprecate(
    tmp_path: Path,
) -> None:
    payload = request(decision("node_a"), rows=[current_row("node_old")])
    runtime = await _run(tmp_path, payload)
    result = runtime.handler_result
    assert isinstance(result, ModelExemplarRegistryReconcileResult)
    assert result == reconcile(payload)
    assert ops(result) == {"node_a": "create", "node_old": "deprecate"}
    assert (
        ModelExemplarRegistryReconcileResult.model_validate_json(
            result.model_dump_json()
        )
        == result
    )


@pytest.mark.asyncio
async def test_error_chain_over_the_bus_fails_without_result(tmp_path: Path) -> None:
    payload = request(decision("node_a"))
    payload["admission"]["repository"] = "omnibase_core"
    (tmp_path / "bad").mkdir()
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(payload))
    refused = RuntimeLocal(
        workflow_path=NODE_DIR / "contract.yaml",
        input_path=bad,
        state_root=tmp_path / "bad",
        backend_overrides={"event_bus": "inmemory"},
        timeout=10,
    )
    assert await refused.run_async() is EnumWorkflowResult.FAILED
    assert refused.handler_result is None
