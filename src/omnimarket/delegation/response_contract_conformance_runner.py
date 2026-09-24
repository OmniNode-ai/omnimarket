# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""L11 response-contract conformance receipts.

The live path invokes the supported ONEX wrapper, on the deployed dev lane or
in this process, and accepts a pass only from its terminal receipt. Fixture
execution is deliberately separate: it exercises receipt validation but cannot
produce an L11 result. A positive receipt requires observed instruction
conveyance and structural deliverable validation; a quality score alone is
never a success condition.

The live result is a conformance bar, not a verdict: every trial grades the
served model's first answer and names the class of any failure, every contract
reports its pass rate and a count per class, and the receipt carries the
invocation that regenerates it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import urlopen
from uuid import NAMESPACE_URL, uuid5

import jsonschema

from omnimarket.delegation.deliverable_extraction import (
    canonical_deliverable_contract_sha256,
    resolve_task_class_deliverable_contract,
)
from omnimarket.delegation.output_only_acceptance import (
    EnumOutputOnlyRefusal,
    evaluate_output_only,
)
from omnimarket.inference.delegation_config_provenance import (
    BIFROST_CONTRACT_CONFIG_KEY,
    BIFROST_OVERLAY_CONFIG_KEY,
    resolve_bifrost_path_binding,
)

LOCUS_DEPLOYED_LANE = "deployed-lane"
LOCUS_IN_PROCESS = "in-process"
_LOCUS_BUS = {LOCUS_DEPLOYED_LANE: "kafka", LOCUS_IN_PROCESS: "inmemory"}

# The acceptance reason a declared-contract rejection carries on the attempt
# record: the gate's contract branch fails deterministically, and a
# deterministic failure is recorded under this reason.
_CONTRACT_REJECTION_REASON = "deterministic_floor_failed"

_SLOT_POLL_SECONDS = 15

# Why a trial did not pass, by the part of the chain it indicts. ``model_*``
# are the served model's own results, ``delivery`` is our path mangling or
# withholding the contract around a model answer, and ``run`` means the
# served model's answer was never graded, so the trial is not a measurement.
# The D1 output-only bar (OMN-18932) judges an otherwise conformant answer:
# ``output_only_refused`` is the served model returning more than the artifact,
# and ``output_only_evidence_absent`` is a trial whose evidence could not decide
# the bar (no raw provider bytes, or a JSON answer's unobservable trailing half),
# so it fails the bar but is not scored as a model result.
_FAILURE_FAMILY: dict[str, str] = {
    "contract_nonconformant": "model_contract",
    "quality_gate_miss": "model_quality",
    "output_bar_nonconformant": "delivery",
    "output_only_refused": "model_output_only",
    "contract_not_conveyed": "delivery",
    "contract_identity_mismatch": "delivery",
    "served_model_not_observed": "run",
    "served_model_call_failed": "run",
    "budget_not_honoured": "run",
    "terminal_evidence_absent": "run",
    "terminal_contract_evidence_absent": "run",
    "predispatch_budget_refusal": "run",
    "invalid_budget_refusal": "run",
    "wrapper_nonzero": "run",
    "wrapper_non_json": "run",
    "output_only_evidence_absent": "run",
}
FAILURE_CLASSES: tuple[str, ...] = tuple(_FAILURE_FAMILY)

# The bar's refusals that say the evidence could not decide it, as opposed to
# a refusal of what the served model returned.
_OUTPUT_ONLY_EVIDENCE_REFUSALS: frozenset[EnumOutputOnlyRefusal] = frozenset(
    {
        EnumOutputOnlyRefusal.RAW_PROVIDER_BYTES_ABSENT,
        EnumOutputOnlyRefusal.EXTRACTION_EVIDENCE_INCOMPLETE,
    }
)


def manifest_sha256(manifest: dict[str, object]) -> str:
    """Return the stable identity of a conformance manifest."""
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def run_manifest(manifest: dict[str, object]) -> dict[str, object]:
    """Run fixture-only trials and return a deterministic machine receipt."""
    if manifest.get("local_only") is not True:
        raise ValueError(
            "response-contract conformance runner accepts local_only manifests"
        )
    manifest_id = _require_string(manifest, "manifest_id")
    contracts = manifest.get("contracts")
    if not isinstance(contracts, list) or not contracts:
        raise ValueError("manifest contracts must be a non-empty list")
    fingerprint = manifest_sha256(manifest)
    contract_receipts = [
        _run_contract(manifest_id, fingerprint, contract)
        for contract in contracts
        if isinstance(contract, dict)
    ]
    if len(contract_receipts) != len(contracts):
        raise ValueError("every manifest contract must be an object")
    passed = all(receipt["passed"] is True for receipt in contract_receipts)
    return {
        "manifest_id": manifest_id,
        "manifest_sha256": fingerprint,
        "local_only": True,
        "contracts": contract_receipts,
        "passed": passed,
    }


def run_live_manifest(
    manifest: dict[str, object],
    *,
    timeout_seconds: int,
    locus: str = LOCUS_DEPLOYED_LANE,
    expected_endpoint_host: str | None = None,
    trials_override: int | None = None,
    slot_guard: SlotGuard | None = None,
    invocation_argv: list[str] | None = None,
    manifest_path: str | None = None,
    source_revision: str | None = None,
) -> dict[str, object]:
    """Run manifest trials against the served model and grade each answer.

    This is the L11 outcome. It refuses to invent evidence when the wrapper
    cannot supply a terminal carrying response-contract evidence.

    ``locus`` chooses where the delegate orchestrator runs: the deployed dev
    lane (the default, unchanged), or this process, where a per-run routing
    overlay (``BIFROST_OVERLAY_PATH``) chooses which lab host serves the local
    rungs. ``expected_endpoint_host`` then refuses an answer from any other
    host as not measured. ``slot_guard`` holds each send until the served
    model's server has a free slot, so a conformance run never crowds out
    another reader of the same server.

    Each trial grades the served model's FIRST answer and names why it failed
    (``FAILURE_CLASSES``), each contract carries a numeric rate with its
    counts, and the receipt records the invocation that regenerates it.
    """
    if manifest.get("local_only") is not True:
        raise ValueError(
            "response-contract conformance runner accepts local_only manifests"
        )
    if timeout_seconds < 1:
        raise ValueError("timeout_seconds must be positive")
    if locus not in _LOCUS_BUS:
        raise ValueError(f"locus must be one of {sorted(_LOCUS_BUS)}, got {locus!r}")
    if trials_override is not None and trials_override < 1:
        raise ValueError("trials_override must be a positive integer")
    manifest_id = _require_string(manifest, "manifest_id")
    contracts = manifest.get("contracts")
    if not isinstance(contracts, list) or not contracts:
        raise ValueError("manifest contracts must be a non-empty list")
    if not all(isinstance(contract, dict) for contract in contracts):
        raise ValueError("every manifest contract must be an object")
    workspace_root, wrapper = _resolve_live_workspace_root()
    started_at = _utc_now()
    contract_receipts = [
        _run_live_contract(
            wrapper,
            workspace_root,
            contract,
            timeout_seconds,
            locus=locus,
            expected_endpoint_host=expected_endpoint_host,
            trials_override=trials_override,
            slot_guard=slot_guard,
        )
        for contract in contracts
        if isinstance(contract, dict)
    ]
    fingerprint = manifest_sha256(manifest)
    return {
        "manifest_id": manifest_id,
        "manifest_sha256": fingerprint,
        "local_only": True,
        "mode": "deployed_lane" if locus == LOCUS_DEPLOYED_LANE else "in_process",
        "contracts": contract_receipts,
        "passed": all(receipt["passed"] is True for receipt in contract_receipts),
        "invocation": {
            "argv": list(invocation_argv) if invocation_argv is not None else None,
            "manifest_path": manifest_path,
            "manifest_sha256": fingerprint,
            "locus": locus,
            "bus": _LOCUS_BUS[locus],
            "timeout_seconds": timeout_seconds,
            "trials_override": trials_override,
            "expected_endpoint_host": expected_endpoint_host,
            **_routing_bindings(),
            "slot_guard": None
            if slot_guard is None
            else {
                "url": slot_guard.url,
                "max_busy": slot_guard.max_busy,
                "wait_seconds": slot_guard.wait_seconds,
            },
            "source_revision": source_revision,
            "started_at": started_at,
            "finished_at": _utc_now(),
        },
    }


def _workspace_root_from_repository() -> Path:
    """Resolve this source checkout's authority for workspace-scoped commands."""
    return _workspace_root_from_source_root(Path(__file__).resolve().parents[3])


def _workspace_root_from_source_root(source_root: Path) -> Path:
    """Resolve a source checkout root; installed-wheel execution is refused."""
    source_root = source_root.resolve()
    if (
        source_root.name != "omnimarket"
        or not (source_root / "pyproject.toml").is_file()
    ):
        raise ValueError(
            "live conformance runner requires an omnimarket source checkout"
        )
    for parent in source_root.parents:
        if parent.name == "omni_worktrees":
            return parent.parent
    return source_root.parent


def _resolve_live_workspace_root() -> tuple[Path, Path]:
    """Return the repository-authorized workspace root and its ONEX wrapper."""
    raw_workspace_root = os.environ.get(
        "OMNI_HOME"
    )  # ONEX_FLAG_EXEMPT: validated CLI workspace root
    if not raw_workspace_root:
        raise ValueError("OMNI_HOME must name the workspace for live conformance")
    workspace_root = Path(raw_workspace_root).expanduser().resolve()
    trusted_workspace_root = _workspace_root_from_repository().resolve()
    if workspace_root != trusted_workspace_root:
        raise ValueError("OMNI_HOME must equal this runner's trusted workspace root")
    wrapper = workspace_root / "omnibase_infra" / "scripts" / "onex"
    resolved_wrapper = wrapper.resolve()
    if not resolved_wrapper.is_file() or not resolved_wrapper.is_relative_to(
        workspace_root
    ):
        raise ValueError("trusted workspace root has no contained ONEX wrapper")
    return workspace_root, resolved_wrapper


def _run_live_contract(
    wrapper: Path,
    workspace_root: Path,
    contract: dict[str, object],
    timeout_seconds: int,
    *,
    locus: str,
    expected_endpoint_host: str | None,
    trials_override: int | None,
    slot_guard: SlotGuard | None,
) -> dict[str, object]:
    contract_id = _require_string(contract, "contract_id")
    task_type = _require_string(contract, "task_type")
    prompt = _require_string(contract, "prompt")
    expected_model = _require_string(contract, "expected_model")
    returned_content_pattern = _require_string(contract, "returned_content_pattern")
    output_shape = _require_string(contract, "output_shape")
    response_contract = contract.get("response_contract")
    if not isinstance(response_contract, dict):
        raise ValueError(f"contract {contract_id}: response_contract must be an object")
    trials = (
        trials_override
        if trials_override is not None
        else _require_positive_int(contract, "trials")
    )
    minimum_pass_rate = _require_fraction(contract, "minimum_pass_rate")
    command = _delegate_command(
        wrapper,
        workspace_root,
        task_type,
        prompt,
        response_contract,
        timeout_seconds,
        locus,
    )
    grading = _TrialGrading(
        task_type=task_type,
        response_contract=response_contract,
        output_shape=output_shape,
        returned_content_pattern=returned_content_pattern,
        expected_model=expected_model,
        expected_endpoint_host=expected_endpoint_host,
    )
    trial_receipts: list[dict[str, object]] = []
    for index in range(trials):
        if slot_guard is not None:
            _wait_for_free_slot(slot_guard)
        trial_receipts.append(_run_live_trial(command, grading, index))
    conformant = sum(item["passed"] is True for item in trial_receipts)
    measured = sum(item.get("failure_family") != "run" for item in trial_receipts)
    failure_counts = dict.fromkeys(FAILURE_CLASSES, 0)
    for item in trial_receipts:
        failure_class = item.get("failure_class")
        if isinstance(failure_class, str):
            failure_counts[failure_class] += 1
    pass_rate = conformant / trials
    return {
        "contract_id": contract_id,
        "task_type": task_type,
        "output_shape": output_shape,
        "expected_model": expected_model,
        "delegate_argv": _portable_argv(command, workspace_root),
        "trials": trial_receipts,
        "trials_run": trials,
        "conformant_trials": conformant,
        "measured_trials": measured,
        # A trial that never reached the served model counts as not meeting the
        # bar in ``pass_rate`` and is reported by count. It is never scored as
        # a model result, so the measured-only rate is None when nothing was.
        "measured_pass_rate": conformant / measured if measured else None,
        "failure_counts": failure_counts,
        "minimum_pass_rate": minimum_pass_rate,
        "pass_rate": pass_rate,
        "passed": pass_rate >= minimum_pass_rate,
    }


def _delegate_command(
    wrapper: Path,
    workspace_root: Path,
    task_type: str,
    prompt: str,
    response_contract: dict[str, object],
    timeout_seconds: int,
    locus: str,
) -> list[str]:
    command = [
        "bash",
        str(wrapper),
        "delegate",
        prompt,
        "--task-type",
        task_type,
        "--response-contract",
        json.dumps(response_contract, sort_keys=True, separators=(",", ":")),
        "--bus",
        _LOCUS_BUS[locus],
    ]
    if locus == LOCUS_DEPLOYED_LANE:
        command += ["--lane", "dev"]
    return [
        *command,
        "--locus",
        locus,
        "--omnibase-path",
        str(workspace_root),
        "--state-root",
        str(workspace_root / ".onex_state"),
        "--timeout",
        str(timeout_seconds),
    ]


@dataclass(frozen=True)
class _TrialGrading:
    task_type: str
    response_contract: dict[str, object]
    output_shape: str
    returned_content_pattern: str
    expected_model: str
    expected_endpoint_host: str | None


def _run_live_trial(
    command: list[str], grading: _TrialGrading, trial_index: int
) -> dict[str, object]:
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return _failed_trial(
            trial_index,
            None,
            "wrapper_nonzero" if completed.returncode != 0 else "wrapper_non_json",
            wrapper_exit_code=completed.returncode,
            **_bounded_wrapper_output(completed.stdout, completed.stderr),
        )
    delegate_run_id = payload.get("run_id") if isinstance(payload, dict) else None
    terminal = _find_terminal(payload)
    if terminal is None:
        return _failed_trial(trial_index, None, "terminal_evidence_absent")
    refusal = terminal.get("budget_refusal")
    if isinstance(refusal, dict):
        receipt = _predispatch_budget_refusal_receipt(
            terminal, refusal, grading.task_type, trial_index
        )
        receipt["wrapper_exit_code"] = completed.returncode
        return receipt
    run_id_value = terminal.get("run_id") or terminal.get("correlation_id")
    run_id = run_id_value if isinstance(run_id_value, str) else None
    # The wrapper exits non-zero on every FAILED terminal, including one whose
    # every local rung refused the contract. That terminal still carries the
    # served model's graded answer, so it is graded first: reading the exit
    # code first filed a model's contract failure as a wrapper failure, which
    # is not a model measurement (measured on the lab host, 2026-09-23). Only a
    # non-zero exit around an otherwise conforming terminal stays a wrapper
    # failure, because nothing may pass on a failed command.
    receipt = _grade_terminal(terminal, grading, trial_index, run_id)
    receipt["delegate_run_id"] = (
        delegate_run_id if isinstance(delegate_run_id, str) else None
    )
    receipt["wrapper_exit_code"] = completed.returncode
    if completed.returncode != 0 and receipt["passed"] is True:
        receipt.update(_bounded_wrapper_output(completed.stdout, completed.stderr))
        return _classified(receipt, "wrapper_nonzero")
    return receipt


def _grade_terminal(
    terminal: dict[str, object],
    grading: _TrialGrading,
    trial_index: int,
    run_id: str | None,
) -> dict[str, object]:
    """Grade the served model's FIRST answer in one terminal.

    With a declared response contract the quality gate IS the contract check:
    a declared contract replaces the task-class rubric, and every contract
    failure is deterministic, recorded on the attempt as
    ``deterministic_floor_failed``. So a rejected first local answer is a
    contract failure when that is its reason, and a quality-gate miss
    otherwise, whichever rung answered after it.
    """
    provider = terminal.get("provider")
    served_endpoint = provider if isinstance(provider, str) else None
    gate_reasons = terminal.get("quality_gates_failed")
    base: dict[str, object] = {
        "trial_index": trial_index,
        "run_id": run_id,
        "served_endpoint": served_endpoint,
        "terminal_status": terminal.get("status"),
        "quality_gate_passed": terminal.get("quality_gate_passed") is True,
        # The last graded attempt's gate reasons and any output refusal, kept so
        # a contract failure says what the check saw, not only that it failed.
        "terminal_gate_reasons": list(gate_reasons)
        if isinstance(gate_reasons, list)
        else [],
        "output_refusal": terminal.get("output_refusal"),
    }
    attempts = terminal.get("attempts")
    local = [
        attempt
        for attempt in (attempts if isinstance(attempts, list) else [])
        if isinstance(attempt, dict)
        and attempt.get("tier") == "local"
        and attempt.get("model_id") == grading.expected_model
    ]
    if not local:
        return _classified(base, "served_model_not_observed")
    first = local[0]
    base["local_attempts"] = len(local)
    base["local_acceptance_reason"] = first.get("acceptance_reason")
    base["local_acceptance_detail"] = first.get("acceptance_detail")
    base["local_failure_class"] = first.get("failure_class")
    decision = first.get("acceptance_decision")
    if first.get("failure_class") is not None or decision is None:
        return _classified(base, "served_model_call_failed")
    if decision != "accept":
        if first.get("acceptance_reason") != _CONTRACT_REJECTION_REASON:
            return _classified(base, "quality_gate_miss")
        return _classified(
            base,
            _unshown_contract_class(terminal.get("response_contract_evidence"), grading)
            or "contract_nonconformant",
        )
    if terminal.get("model_name") != grading.expected_model or (
        grading.expected_endpoint_host is not None
        and (
            served_endpoint is None
            or urlparse(served_endpoint).hostname != grading.expected_endpoint_host
        )
    ):
        return _classified(base, "served_model_not_observed")
    evidence = terminal.get("response_contract_evidence")
    budget_evidence = terminal.get("budget_evidence")
    if not isinstance(evidence, dict) or not isinstance(budget_evidence, dict):
        return _classified(base, "terminal_contract_evidence_absent")
    budget_honoured = _budget_is_honoured(budget_evidence)
    conveyed = evidence.get("conveyed") is True
    validated = evidence.get("validated") is True
    channel = evidence.get("channel")
    resolved_contract = resolve_task_class_deliverable_contract(
        grading.task_type, grading.response_contract
    )
    resolved_output_shape = resolved_contract.output_shape.value
    content = terminal.get("response")
    returned_content_valid = isinstance(content, str) and _returned_content_validates(
        content,
        resolved_output_shape,
        grading.response_contract,
        list(resolved_contract.markers),
        grading.returned_content_pattern,
    )
    preamble_chars = terminal.get("preamble_chars")
    preamble_evidence_valid = isinstance(preamble_chars, int) and preamble_chars >= 0
    # OMN-18932 (K5, D1): the output-only release bar. The terminal carries the
    # caller's bytes and the runtime's count of leading characters it cut
    # (``preamble_chars``), but no raw provider response, so the "no extraction"
    # half is judged from that count. It proves a text deliverable was not
    # extracted; a JSON deliverable's trailing half is unobservable from it and
    # the bar refuses. K5's final evidence still needs the raw response.
    output_only = evaluate_output_only(
        raw_response=None,
        caller_bytes=content if isinstance(content, str) else "",
        contract=resolved_contract,
        runtime_leading_chars=(
            preamble_chars
            if isinstance(preamble_chars, int) and preamble_chars >= 0
            else None
        ),
    )
    base.update(
        {
            "conveyed": conveyed,
            "validated": validated,
            "channel": channel,
            "raw_preamble_chars": preamble_chars,
            "budget_honoured": budget_honoured,
            "local_model_observed": True,
            "preamble_evidence_valid": preamble_evidence_valid,
            "returned_content_valid": returned_content_valid,
            "output_only": output_only.model_dump(mode="json"),
        }
    )
    if not budget_honoured:
        return _classified(base, "budget_not_honoured")
    if not conveyed or not isinstance(channel, str) or not channel:
        return _classified(base, "contract_not_conveyed")
    if (
        grading.output_shape != resolved_output_shape
        or evidence.get("contract_sha256")
        != canonical_deliverable_contract_sha256(resolved_contract)
        or evidence.get("output_shape") != resolved_output_shape
    ):
        return _classified(base, "contract_identity_mismatch")
    if not validated:
        return _classified(base, "contract_nonconformant")
    if not preamble_evidence_valid or not returned_content_valid:
        return _classified(base, "output_bar_nonconformant")
    if not output_only.accepted:
        return _classified(
            base,
            "output_only_evidence_absent"
            if set(output_only.refusals) <= _OUTPUT_ONLY_EVIDENCE_REFUSALS
            else "output_only_refused",
        )
    return _classified(base, None)


def _unshown_contract_class(evidence: object, grading: _TrialGrading) -> str | None:
    """Why a contract rejection does not indict the model, or None if it does.

    A rejected answer is the model failing the contract only when the terminal
    shows that contract, the one the manifest declared, reached the model. The
    2026-09-18 twelve-of-twelve was the gate holding a contract the model never
    saw; graded on the rejection reason alone it reads as ``model_contract``.
    """
    if not isinstance(evidence, dict):
        return "terminal_contract_evidence_absent"
    channel = evidence.get("channel")
    if (
        evidence.get("conveyed") is not True
        or not isinstance(channel, str)
        or not channel
    ):
        return "contract_not_conveyed"
    resolved = resolve_task_class_deliverable_contract(
        grading.task_type, grading.response_contract
    )
    shape = resolved.output_shape.value
    if (
        grading.output_shape != shape
        or evidence.get("output_shape") != shape
        or evidence.get("contract_sha256")
        != canonical_deliverable_contract_sha256(resolved)
    ):
        return "contract_identity_mismatch"
    return None


def _classified(
    receipt: dict[str, object], failure_class: str | None
) -> dict[str, object]:
    receipt["failure_class"] = failure_class
    receipt["failure_family"] = (
        None if failure_class is None else _FAILURE_FAMILY[failure_class]
    )
    receipt["passed"] = failure_class is None
    return receipt


def _failed_trial(
    trial_index: int, run_id: str | None, failure_class: str, **extra: object
) -> dict[str, object]:
    return _classified(
        {"trial_index": trial_index, "run_id": run_id, **extra}, failure_class
    )


@dataclass(frozen=True)
class SlotGuard:
    """Hold each send until the served model's server has a free slot.

    ``url`` is a llama.cpp-style ``/slots`` endpoint returning a list of slot
    objects, each with a boolean ``is_processing``. A send waits while at least
    ``max_busy`` slots are busy, and the run aborts, sending nothing more, when
    no slot frees within ``wait_seconds``.
    """

    url: str
    max_busy: int
    wait_seconds: int

    def __post_init__(self) -> None:
        if self.max_busy < 1 or self.wait_seconds < 1:
            raise ValueError("slot guard max_busy and wait_seconds must be positive")


def _read_busy_slots(url: str) -> int:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(f"slot guard url must be http(s), got {url!r}")
    with urlopen(url, timeout=10) as response:
        slots = json.loads(response.read().decode())
    if not isinstance(slots, list):
        raise RuntimeError(f"slot endpoint {url} did not return a list")
    return sum(
        1 for slot in slots if isinstance(slot, dict) and slot.get("is_processing")
    )


def _wait_for_free_slot(guard: SlotGuard) -> None:
    deadline = time.monotonic() + guard.wait_seconds
    while True:
        busy = _read_busy_slots(guard.url)
        if busy < guard.max_busy:
            return
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"served model server stayed busy ({busy} slots >= {guard.max_busy})"
                f" for {guard.wait_seconds}s; conformance run aborted"
            )
        time.sleep(_SLOT_POLL_SECONDS)


def _portable_argv(command: list[str], workspace_root: Path) -> list[str]:
    """Write the delegate command without this machine's workspace path."""
    root = str(workspace_root)
    return [
        "$OMNI_HOME" + arg[len(root) :] if arg.startswith(root) else arg
        for arg in command
    ]


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _routing_bindings() -> dict[str, dict[str, object] | None]:
    """Name the routing contract and overlay this run's delegates resolve.

    Read through the one bifrost binding seam the delegate path itself uses,
    and recorded by file name and content hash, so the receipt says which
    routing chose the served model without carrying a machine-local path.
    """
    binding = resolve_bifrost_path_binding()
    return {
        "routing_contract": _bound_file_identity(
            BIFROST_CONTRACT_CONFIG_KEY, binding.contract_path
        ),
        "overlay": _bound_file_identity(
            BIFROST_OVERLAY_CONFIG_KEY, binding.overlay_path
        ),
    }


def _bound_file_identity(key: str, bound: Path | None) -> dict[str, object] | None:
    if bound is None:
        return None
    path = bound.expanduser()
    return {
        "env": key,
        "file_name": path.name,
        "sha256": _file_sha256(path) if path.is_file() else None,
    }


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _find_terminal(payload: object) -> dict[str, object] | None:
    if isinstance(payload, dict):
        if "response_contract_evidence" in payload or "budget_refusal" in payload:
            return payload
        for value in payload.values():
            found = _find_terminal(value)
            if found is not None:
                return found
    if isinstance(payload, list):
        for value in payload:
            found = _find_terminal(value)
            if found is not None:
                return found
    return None


def _predispatch_budget_refusal_receipt(
    terminal: dict[str, object],
    refusal: dict[str, object],
    task_type: str,
    trial_index: int,
) -> dict[str, object]:
    """Record a typed pre-execution budget refusal as a non-pass trial."""
    run_id = terminal.get("run_id") or terminal.get("correlation_id")
    requested_timeout_seconds = refusal.get("requested_timeout_seconds")
    task_class_timeout_ceiling_seconds = refusal.get(
        "task_class_timeout_ceiling_seconds"
    )
    valid_refusal = (
        terminal.get("status") == "failed"
        and refusal.get("reason") == "timeout_exceeds_task_class_ceiling"
        and refusal.get("task_type") == task_type
        and isinstance(requested_timeout_seconds, int)
        and requested_timeout_seconds > 0
        and isinstance(task_class_timeout_ceiling_seconds, int)
        and task_class_timeout_ceiling_seconds > 0
        and requested_timeout_seconds > task_class_timeout_ceiling_seconds
    )
    return _failed_trial(
        trial_index,
        run_id if isinstance(run_id, str) else None,
        "predispatch_budget_refusal" if valid_refusal else "invalid_budget_refusal",
        budget_refusal=refusal,
    )


def _budget_is_honoured(evidence: dict[str, object]) -> bool:
    requested = evidence.get("requested_timeout_seconds")
    ceiling = evidence.get("task_class_timeout_ceiling_seconds")
    execution = evidence.get("execution_timeout_seconds")
    margin = evidence.get("terminal_delivery_margin_seconds")
    values = (requested, ceiling, execution, margin)
    if not all(isinstance(value, int) and value > 0 for value in values):
        return False
    assert isinstance(requested, int)
    assert isinstance(ceiling, int)
    assert isinstance(execution, int)
    return requested <= ceiling and execution == requested


def _bounded_wrapper_output(stdout: str, stderr: str) -> dict[str, object]:
    """Keep bounded diagnostic evidence when the wrapper cannot yield a receipt."""
    limit = 1_024
    return {
        "stdout_sha256": hashlib.sha256(stdout.encode()).hexdigest(),
        "stderr_sha256": hashlib.sha256(stderr.encode()).hexdigest(),
        "stdout_excerpt": stdout[:limit],
        "stderr_excerpt": stderr[:limit],
        "output_truncated": len(stdout) > limit or len(stderr) > limit,
    }


def _returned_content_validates(
    content: str,
    output_shape: str,
    response_contract: dict[str, object],
    markers: object,
    returned_content_pattern: str,
) -> bool:
    if output_shape == "json":
        return (
            _extract_deliverable(content, output_shape, response_contract, markers)
            == content
        )
    return re.fullmatch(returned_content_pattern, content) is not None


def _run_contract(
    manifest_id: str, fingerprint: str, contract: dict[str, object]
) -> dict[str, object]:
    contract_id = _require_string(contract, "contract_id")
    output_shape = _require_string(contract, "output_shape")
    if output_shape not in {"json", "markdown", "plain_text"}:
        raise ValueError(f"contract {contract_id}: unsupported output_shape")
    minimum_pass_rate = _require_fraction(contract, "minimum_pass_rate")
    minimum_deliverable_share = _require_fraction(contract, "minimum_deliverable_share")
    trial_count = _require_positive_int(contract, "trials")
    instruction = _require_string(contract, "instruction")
    provider_capture = contract.get("provider_request_capture")
    if not isinstance(provider_capture, dict):
        raise ValueError(
            f"contract {contract_id}: provider_request_capture must be an object"
        )
    sample = contract.get("sample")
    if not isinstance(sample, dict):
        raise ValueError(f"contract {contract_id}: sample must be an object")

    trial_receipts = [
        _run_trial(
            manifest_id=manifest_id,
            manifest_sha=fingerprint,
            contract_id=contract_id,
            trial_index=index,
            output_shape=output_shape,
            response_contract=contract.get("response_contract"),
            markers=contract.get("markers"),
            minimum_deliverable_share=minimum_deliverable_share,
            instruction=instruction,
            provider_capture=provider_capture,
            raw_response=_require_string(sample, "raw_response"),
        )
        for index in range(trial_count)
    ]
    passed_count = sum(receipt["passed"] is True for receipt in trial_receipts)
    pass_rate = passed_count / trial_count
    negative = contract.get("conveyance_disabled_negative_control")
    negative_receipt = (
        _run_negative_control(negative, output_shape, contract.get("response_contract"))
        if isinstance(negative, dict)
        else None
    )
    return {
        "contract_id": contract_id,
        "output_shape": output_shape,
        "minimum_pass_rate": minimum_pass_rate,
        "pass_rate": pass_rate,
        "trials": trial_receipts,
        "negative_control": negative_receipt,
        "passed": pass_rate >= minimum_pass_rate
        and (negative_receipt is None or negative_receipt["passed"] is True),
    }


def _run_trial(
    *,
    manifest_id: str,
    manifest_sha: str,
    contract_id: str,
    trial_index: int,
    output_shape: str,
    response_contract: object,
    markers: object,
    minimum_deliverable_share: float,
    instruction: str,
    provider_capture: dict[str, object],
    raw_response: str,
) -> dict[str, object]:
    captured = _captured_channel_content(provider_capture)
    conveyed = instruction in captured
    deliverable = _extract_deliverable(
        raw_response, output_shape, response_contract, markers
    )
    share = len(deliverable) / len(raw_response) if raw_response else 0.0
    validated = bool(deliverable) and _validates(
        deliverable, output_shape, response_contract
    )
    passed = conveyed and validated and share >= minimum_deliverable_share
    run_id = str(
        uuid5(
            NAMESPACE_URL, f"{manifest_id}:{manifest_sha}:{contract_id}:{trial_index}"
        )
    )
    return {
        "run_id": run_id,
        "trial_index": trial_index,
        "channel": _require_string(provider_capture, "channel"),
        "conveyed": conveyed,
        "validated": validated,
        "preamble_chars": len(raw_response) - len(deliverable),
        "deliverable_share": share,
        "passed": passed,
    }


def _run_negative_control(
    control: dict[str, object], output_shape: str, response_contract: object
) -> dict[str, object]:
    raw_response = _require_string(control, "raw_response")
    expected_validated = control.get("expected_validated")
    if not isinstance(expected_validated, bool):
        raise ValueError("negative control expected_validated must be boolean")
    deliverable = _extract_deliverable(
        raw_response, output_shape, response_contract, ()
    )
    validated = bool(deliverable) and _validates(
        deliverable, output_shape, response_contract
    )
    return {
        "mode": "conveyance_disabled",
        "validated": validated,
        "passed": validated is expected_validated,
    }


def _captured_channel_content(capture: dict[str, object]) -> str:
    channel = _require_string(capture, "channel")
    payload = capture.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("provider_request_capture payload must be an object")
    if channel != "messages[0].content":
        raise ValueError(f"unsupported captured provider channel: {channel}")
    messages = payload.get("messages")
    if (
        not isinstance(messages, list)
        or not messages
        or not isinstance(messages[0], dict)
    ):
        raise ValueError("captured provider payload must contain messages[0]")
    content = messages[0].get("content")
    if not isinstance(content, str):
        raise ValueError("captured provider system message must contain string content")
    return content


def _extract_deliverable(
    raw_response: str,
    output_shape: str,
    response_contract: object,
    markers: object,
) -> str:
    if output_shape == "json":
        if not isinstance(response_contract, dict):
            raise ValueError("json output_shape requires a response_contract object")
        decoder = json.JSONDecoder()
        validator = jsonschema.validators.validator_for(response_contract)(
            response_contract
        )
        candidates: list[tuple[int, int]] = []
        for index, character in enumerate(raw_response):
            if character not in "{[":
                continue
            try:
                value, end = decoder.raw_decode(raw_response, index)
            except json.JSONDecodeError:
                continue
            if not list(validator.iter_errors(value)):
                candidates.append((index, end))
        if not candidates:
            return ""
        start, end = candidates[-1]
        return raw_response[start:end]
    if not isinstance(markers, list) or not all(
        isinstance(marker, str) for marker in markers
    ):
        raise ValueError("text output shapes require a marker list")
    offset = 0
    boundary: int | None = None
    for line in raw_response.splitlines(keepends=True):
        if line.strip() in markers:
            boundary = offset + len(line)
        offset += len(line)
    return "" if boundary is None else raw_response[boundary:].lstrip()


def _validates(deliverable: str, output_shape: str, response_contract: object) -> bool:
    if output_shape != "json":
        return bool(deliverable.strip())
    if not isinstance(response_contract, dict):
        return False
    try:
        candidate = json.loads(deliverable)
    except json.JSONDecodeError:
        return False
    validator = jsonschema.validators.validator_for(response_contract)(
        response_contract
    )
    return not list(validator.iter_errors(candidate))


def _require_string(value: dict[str, object], key: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result:
        raise ValueError(f"{key} must be a non-empty string")
    return result


def _require_positive_int(value: dict[str, object], key: str) -> int:
    result = value.get(key)
    if not isinstance(result, int) or result < 1:
        raise ValueError(f"{key} must be a positive integer")
    return result


def _require_fraction(value: dict[str, object], key: str) -> float:
    result = value.get(key)
    if not isinstance(result, (int, float)) or not 0.0 < float(result) <= 1.0:
        raise ValueError(f"{key} must be in (0, 1]")
    return float(result)
