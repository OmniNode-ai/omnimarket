# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_receipt_gate_pin_advance_compute [OMN-20134].

The callers' pins are contract data. A stale pin is detected (the planted
violation), the plan edits the pin line and its test constant together, a caller
whose pinned tag carries the newest gate gets no pull request, and every way the
declared data and a repository can disagree is a typed verdict with no edits.
"""

from __future__ import annotations

import ast
import importlib
import re
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_github_repo_write_effect.models.model_repo_write_io import (
    EnumRepoWriteOperation,
    ModelRepoWriteRequest,
)
from omnimarket.nodes.node_receipt_gate_pin_advance_compute.handlers.handler_receipt_gate_pin_advance import (
    HandlerReceiptGatePinAdvance,
)
from omnimarket.nodes.node_receipt_gate_pin_advance_compute.models import (
    EnumCallerVerdict,
    EnumPinForm,
    ModelCallerPlan,
    ModelCallerSnapshot,
    ModelCoreDevTag,
    ModelPinSite,
    ModelReceiptGatePinAdvanceRequest,
    ModelReceiptGatePinAdvanceResult,
    ModelReceiptGatePinPolicy,
)

pytestmark = pytest.mark.unit

NODE_NAME = "node_receipt_gate_pin_advance_compute"
NODE_DIR = (
    Path(__file__).resolve().parents[3] / "src" / "omnimarket" / "nodes" / NODE_NAME
)
GATE = "OmniNode-ai/omnibase_core/.github/workflows/receipt-gate.yml"
WORKFLOW = ".github/workflows/call-repo-evidence-gate.yml"
CHECKS = ".github/required-checks.yaml"
HEAD = "c" * 40

OLD = ModelCoreDevTag(
    tag="dev-20261022114039-578228977721",
    commit_sha="5782289777210000000000000000000000000000",
    receipt_gate_blob="a" * 40,
)
MID = ModelCoreDevTag(
    tag="dev-20261030004553-1088a7b03030",
    commit_sha="1088a7b03030000000000000000000000000000f",
    receipt_gate_blob="b" * 40,
)
NEW = ModelCoreDevTag(
    tag="dev-20261030152335-66d35d2283c3",
    commit_sha="66d35d2283c3011c436799eacb9bce1a434ee6c9",
    receipt_gate_blob="d" * 40,
)
NEWER_SAME_GATE = ModelCoreDevTag(
    tag="dev-20261030214334-59faa7f31f38",
    commit_sha="59faa7f31f38000000000000000000000000000a",
    receipt_gate_blob="d" * 40,
)
OLD_SHA = "4e4f5e0404d364e296c5d37db6aa3ab9e07f8ffa"


def load_pin_policy() -> ModelReceiptGatePinPolicy:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
    return ModelReceiptGatePinPolicy.model_validate(contract["receipt_gate_pin_policy"])


@pytest.fixture(scope="module")
def policy() -> ModelReceiptGatePinPolicy:
    return load_pin_policy()


@pytest.fixture(scope="module")
def handler() -> HandlerReceiptGatePinAdvance:
    return HandlerReceiptGatePinAdvance()


def _file_text(path: str, form: EnumPinForm, tag: str, sha: str, *, both: bool) -> str:
    ref = tag if form is EnumPinForm.TAG else sha
    if path == WORKFLOW:
        comment = "" if form is EnumPinForm.TAG else f" # {tag}; the rest is prose"
        return f"jobs:\n  repo-evidence:\n    uses: {GATE}@{ref}{comment}\n"
    if path == CHECKS:
        return f'gates:\n  - cross_repo_ref: "{GATE}@{ref}" # {tag}\n'
    if path.endswith("receipt_gate_pin.py"):
        text = f'RECEIPT_GATE_PIN_TAG = "{tag}"\n'
        return text + (f'RECEIPT_GATE_PIN_SHA = "{sha}"\n' if both else "")
    if path.endswith(".json"):
        return f'[["call-repo-evidence-gate.yml", 52, "{GATE}", "{tag}"]]\n'
    raise AssertionError(path)


def _snapshot(
    policy: ModelReceiptGatePinPolicy,
    repository: str,
    pinned: ModelCoreDevTag,
    *,
    workflow_ref: str | None = None,
) -> ModelCallerSnapshot:
    caller = next(c for c in policy.callers if c.repository == repository)
    both = any(s.ref_form is EnumPinForm.SHA for s in caller.test_constants)
    files: dict[str, str] = {}
    for site in (*caller.pin_sites, *caller.test_constants):
        files[site.path] = (
            files.get(site.path, "")
            if site.path.endswith("receipt_gate_pin.py") and site.path in files
            else _file_text(
                site.path, site.ref_form, pinned.tag, pinned.commit_sha, both=both
            )
        )
    if workflow_ref is not None:
        files[WORKFLOW] = f"jobs:\n  repo-evidence:\n    uses: {GATE}@{workflow_ref}\n"
    return ModelCallerSnapshot(repository=repository, base_head_sha=HEAD, files=files)


def _request(
    policy: ModelReceiptGatePinPolicy,
    pinned: ModelCoreDevTag,
    tags: tuple[ModelCoreDevTag, ...] = (OLD, MID, NEW),
) -> ModelReceiptGatePinAdvanceRequest:
    return ModelReceiptGatePinAdvanceRequest(
        policy=policy,
        core_tags=tags,
        callers=tuple(_snapshot(policy, c.repository, pinned) for c in policy.callers),
    )


def _plan(result: ModelReceiptGatePinAdvanceResult, repo: str) -> ModelCallerPlan:
    return next(p for p in result.plans if p.repository == repo)


def test_contract_declares_the_callers_as_data_and_no_bus_topics() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
    assert contract["name"] == NODE_NAME
    assert contract["node_type"] == "COMPUTE_GENERIC"
    assert contract["event_bus"] == {"subscribe_topics": [], "publish_topics": []}
    assert contract["descriptor"]["purity"] == "pure"
    policy = load_pin_policy()
    assert {c.repository.split("/")[1] for c in policy.callers} == {
        "omnibase_infra",
        "omnibase_spi",
        "omniclaude",
        "omniintelligence",
        "omnimemory",
        "omnimarket",
    }
    assert {e.repository.split("/")[1] for e in policy.excluded} == {
        "omnidash",
        "omnibase_core",
    }
    assert all(e.reason for e in policy.excluded)


def test_handler_is_routed_from_the_contract_and_metadata_registers_the_entry_point() -> (
    None
):
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
    module = importlib.import_module(contract["handler"]["module"])
    assert getattr(module, contract["handler"]["class"]) is HandlerReceiptGatePinAdvance
    metadata = yaml.safe_load((NODE_DIR / "metadata.yaml").read_text())
    assert (
        metadata["entry_points"]["onex.nodes"][NODE_NAME]
        == f"omnimarket.nodes.{NODE_NAME}"
    )


def test_no_pin_site_or_repository_is_hard_coded_in_the_python() -> None:
    literals: list[str] = []
    for path in (NODE_DIR / "handlers").glob("*.py"):
        tree = ast.parse(path.read_text())
        docstrings = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef)
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
        }
        literals += [
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ]
    text = "\n".join(literals)
    assert "OmniNode-ai" not in text
    assert not re.search(r"omni(?:base_|market|claude|memory|intelligence)", text)
    assert not re.search(r"[0-9a-f]{40}", text)


def test_planted_stale_pin_is_detected_at_every_caller(
    handler: HandlerReceiptGatePinAdvance, policy: ModelReceiptGatePinPolicy
) -> None:
    result = handler.handle(_request(policy, OLD))
    assert result.newest_tag == NEW.tag
    assert len(result.plans) == len(policy.callers)
    assert {p.verdict for p in result.plans} == {EnumCallerVerdict.STALE}
    assert result.violations == result.plans
    assert all(p.target_tag == NEW.tag for p in result.plans)
    assert all(p.current_tag == OLD.tag for p in result.plans)


def test_pin_line_and_test_constant_are_edited_together_in_one_plan(
    handler: HandlerReceiptGatePinAdvance, policy: ModelReceiptGatePinPolicy
) -> None:
    result = handler.handle(_request(policy, OLD))
    market = _plan(result, "OmniNode-ai/omnimarket")
    assert {e.path for e in market.edits} == {
        WORKFLOW,
        "tests/ci/receipt_gate_pin.py",
        "tests/ci/fixtures/omn20558_workflow_action_refs.json",
    }
    by_path = {e.path: e.new_text for e in market.edits}
    assert f"{GATE}@{NEW.tag}" in by_path[WORKFLOW]
    assert OLD.tag not in "".join(by_path.values())
    assert (
        f'RECEIPT_GATE_PIN_TAG = "{NEW.tag}"' in by_path["tests/ci/receipt_gate_pin.py"]
    )
    assert market.branch == f"pin-advance/receipt-gate/{NEW.tag}"
    assert (
        market.idempotency_key == f"receipt-gate-pin:OmniNode-ai.omnimarket:{NEW.tag}"
    )
    assert policy.ticket in market.title
    assert policy.ticket in market.body
    assert market.commit_message.startswith(market.title)


def test_sha_form_caller_keeps_a_sha_with_the_tag_named_beside_it(
    handler: HandlerReceiptGatePinAdvance, policy: ModelReceiptGatePinPolicy
) -> None:
    result = handler.handle(_request(policy, OLD))
    infra = _plan(result, "OmniNode-ai/omnibase_infra")
    by_path = {e.path: e.new_text for e in infra.edits}
    assert set(by_path) == {WORKFLOW, CHECKS, "tests/ci/receipt_gate_pin.py"}
    assert (
        f"{GATE}@{NEW.commit_sha} # {NEW.tag}; the rest is prose" in by_path[WORKFLOW]
    )
    assert f'"{GATE}@{NEW.commit_sha}" # {NEW.tag}' in by_path[CHECKS]
    constants = by_path["tests/ci/receipt_gate_pin.py"]
    assert f'RECEIPT_GATE_PIN_TAG = "{NEW.tag}"' in constants
    assert f'RECEIPT_GATE_PIN_SHA = "{NEW.commit_sha}"' in constants


def test_applying_the_plan_reaches_a_fixed_point(
    handler: HandlerReceiptGatePinAdvance, policy: ModelReceiptGatePinPolicy
) -> None:
    before = _request(policy, OLD)
    result = handler.handle(before)
    advanced = []
    for snapshot in before.callers:
        plan = _plan(result, snapshot.repository)
        files = {**snapshot.files, **{e.path: e.new_text for e in plan.edits}}
        advanced.append(snapshot.model_copy(update={"files": files}))
    again = handler.handle(before.model_copy(update={"callers": tuple(advanced)}))
    assert {p.verdict for p in again.plans} == {EnumCallerVerdict.CURRENT}
    assert all(not p.edits for p in again.plans)
    assert again.violations == ()


def test_callers_on_the_newest_gate_get_no_pull_request(
    handler: HandlerReceiptGatePinAdvance, policy: ModelReceiptGatePinPolicy
) -> None:
    result = handler.handle(_request(policy, NEW))
    assert {p.verdict for p in result.plans} == {EnumCallerVerdict.CURRENT}
    assert result.advances == ()
    assert result.violations == ()


def test_a_newer_tag_with_the_same_gate_file_does_not_make_a_pin_stale(
    handler: HandlerReceiptGatePinAdvance, policy: ModelReceiptGatePinPolicy
) -> None:
    result = handler.handle(_request(policy, NEW, (OLD, MID, NEW, NEWER_SAME_GATE)))
    assert result.newest_tag == NEWER_SAME_GATE.tag
    assert {p.verdict for p in result.plans} == {EnumCallerVerdict.CURRENT}


def test_an_intermediate_tag_whose_gate_differs_is_stale_against_the_newest(
    handler: HandlerReceiptGatePinAdvance, policy: ModelReceiptGatePinPolicy
) -> None:
    result = handler.handle(_request(policy, MID))
    assert {p.verdict for p in result.plans} == {EnumCallerVerdict.STALE}
    assert {p.target_tag for p in result.plans} == {NEW.tag}


def test_a_full_sha_where_a_tag_is_declared_is_untagged_even_on_the_newest_gate(
    handler: HandlerReceiptGatePinAdvance, policy: ModelReceiptGatePinPolicy
) -> None:
    spi = "OmniNode-ai/omnibase_spi"
    request = _request(policy, NEW)
    swapped = tuple(
        _snapshot(policy, spi, NEW, workflow_ref=NEW.commit_sha)
        if s.repository == spi
        else s
        for s in request.callers
    )
    result = handler.handle(request.model_copy(update={"callers": swapped}))
    plan = _plan(result, spi)
    assert plan.verdict is EnumCallerVerdict.UNTAGGED
    assert plan.target_tag == NEW.tag
    assert f"{GATE}@{NEW.tag}" in {e.path: e.new_text for e in plan.edits}[WORKFLOW]
    assert all(
        p.verdict is EnumCallerVerdict.CURRENT for p in result.plans if p is not plan
    )


def test_a_sha_or_tag_the_tag_list_does_not_hold_is_unresolved(
    handler: HandlerReceiptGatePinAdvance, policy: ModelReceiptGatePinPolicy
) -> None:
    request = _request(policy, NEW)
    swapped = tuple(
        _snapshot(policy, s.repository, NEW, workflow_ref=OLD_SHA)
        if s.repository == "OmniNode-ai/omniclaude"
        else s
        for s in request.callers
    )
    plan = _plan(
        handler.handle(request.model_copy(update={"callers": swapped})),
        "OmniNode-ai/omniclaude",
    )
    assert plan.verdict is EnumCallerVerdict.UNTAGGED
    unknown = "dev-20260101000000-000000000000"
    swapped = tuple(
        _snapshot(policy, s.repository, NEW, workflow_ref=unknown)
        if s.repository == "OmniNode-ai/omniclaude"
        else s
        for s in request.callers
    )
    plan = _plan(
        handler.handle(request.model_copy(update={"callers": swapped})),
        "OmniNode-ai/omniclaude",
    )
    assert plan.verdict is EnumCallerVerdict.UNRESOLVED
    assert plan.target_tag == NEW.tag


def test_a_test_constant_behind_the_pin_line_is_realigned_alone(
    handler: HandlerReceiptGatePinAdvance, policy: ModelReceiptGatePinPolicy
) -> None:
    memory = "OmniNode-ai/omnimemory"
    request = _request(policy, NEW)
    behind = _snapshot(policy, memory, OLD)
    mixed = behind.model_copy(
        update={
            "files": {
                **behind.files,
                WORKFLOW: _snapshot(policy, memory, NEW).files[WORKFLOW],
            }
        }
    )
    swapped = tuple(mixed if s.repository == memory else s for s in request.callers)
    plan = _plan(
        handler.handle(request.model_copy(update={"callers": swapped})), memory
    )
    assert plan.verdict is EnumCallerVerdict.SITES_DISAGREE
    assert [e.path for e in plan.edits] == ["tests/ci/receipt_gate_pin.py"]
    assert plan.target_tag == NEW.tag
    assert "align" in plan.title


def test_a_site_that_does_not_match_as_declared_refuses_with_no_edits(
    handler: HandlerReceiptGatePinAdvance, policy: ModelReceiptGatePinPolicy
) -> None:
    spi = "OmniNode-ai/omnibase_spi"
    request = _request(policy, OLD)
    base = _snapshot(policy, spi, OLD)
    unmatched = base.model_copy(
        update={"files": {**base.files, WORKFLOW: "jobs: {}\n"}}
    )
    twice = base.model_copy(
        update={"files": {**base.files, WORKFLOW: base.files[WORKFLOW] * 2}}
    )
    gone = base.model_copy(
        update={"files": {k: v for k, v in base.files.items() if k != WORKFLOW}}
    )
    for broken in (unmatched, twice, gone):
        callers = tuple(broken if s.repository == spi else s for s in request.callers)
        plan = _plan(
            handler.handle(request.model_copy(update={"callers": callers})), spi
        )
        assert plan.verdict is EnumCallerVerdict.PIN_SITE_UNMATCHED
        assert plan.edits == ()
        assert plan.needs_pull_request is False


def test_a_declared_caller_with_no_snapshot_is_a_violation_not_a_pass(
    handler: HandlerReceiptGatePinAdvance, policy: ModelReceiptGatePinPolicy
) -> None:
    request = _request(policy, NEW)
    callers = tuple(
        s for s in request.callers if s.repository != "OmniNode-ai/omnimarket"
    )
    result = handler.handle(request.model_copy(update={"callers": callers}))
    plan = _plan(result, "OmniNode-ai/omnimarket")
    assert plan.verdict is EnumCallerVerdict.SNAPSHOT_MISSING
    assert result.violations == (plan,)


def test_a_plan_is_a_valid_pr_create_request_for_the_write_effect(
    handler: HandlerReceiptGatePinAdvance, policy: ModelReceiptGatePinPolicy
) -> None:
    from uuid import uuid4

    for plan in handler.handle(_request(policy, OLD)).advances:
        request = ModelRepoWriteRequest(
            correlation_id=uuid4(),
            operation=EnumRepoWriteOperation.PR_CREATE,
            repo=plan.repository,
            idempotency_key=plan.idempotency_key,
            requested_by_lane="receipt-gate-pin-advance",
            head=plan.branch,
            base=plan.base_branch,
            title=plan.title,
            body=plan.body,
        )
        assert request.head == plan.branch


def test_policy_refuses_a_pin_site_pattern_without_a_ref_group() -> None:
    with pytest.raises(ValueError, match="group 'ref'"):
        ModelPinSite(path="x", pattern=r"@[0-9a-f]{40}", ref_form=EnumPinForm.SHA)
