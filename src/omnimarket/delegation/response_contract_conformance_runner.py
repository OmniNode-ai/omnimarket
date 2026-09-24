# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""L11 response-contract conformance receipts.

The live path invokes the supported deployed-lane wrapper and accepts a pass
only from its terminal receipt. Fixture execution is deliberately separate:
it exercises receipt validation but cannot produce an L11 result. A positive
receipt requires observed instruction conveyance and structural deliverable
validation; a quality score alone is never a success condition.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import jsonschema

from omnimarket.delegation.deliverable_extraction import (
    canonical_deliverable_contract_sha256,
    resolve_task_class_deliverable_contract,
)
from omnimarket.delegation.output_only_acceptance import evaluate_output_only


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
    manifest: dict[str, object], *, timeout_seconds: int
) -> dict[str, object]:
    """Run manifest trials through the deployed-lane wrapper.

    This is the L11 outcome. It refuses to invent evidence when the wrapper
    cannot supply a terminal carrying response-contract evidence.
    """
    if manifest.get("local_only") is not True:
        raise ValueError(
            "response-contract conformance runner accepts local_only manifests"
        )
    if timeout_seconds < 1:
        raise ValueError("timeout_seconds must be positive")
    manifest_id = _require_string(manifest, "manifest_id")
    contracts = manifest.get("contracts")
    if not isinstance(contracts, list) or not contracts:
        raise ValueError("manifest contracts must be a non-empty list")
    workspace_root, wrapper = _resolve_live_workspace_root()
    contract_receipts = [
        _run_live_contract(wrapper, workspace_root, contract, timeout_seconds)
        for contract in contracts
        if isinstance(contract, dict)
    ]
    if len(contract_receipts) != len(contracts):
        raise ValueError("every manifest contract must be an object")
    return {
        "manifest_id": manifest_id,
        "manifest_sha256": manifest_sha256(manifest),
        "local_only": True,
        "mode": "deployed_lane",
        "contracts": contract_receipts,
        "passed": all(receipt["passed"] is True for receipt in contract_receipts),
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
) -> dict[str, object]:
    contract_id = _require_string(contract, "contract_id")
    task_type = _require_string(contract, "task_type")
    prompt = _require_string(contract, "prompt")
    expected_model = _require_string(contract, "expected_model")
    returned_content_pattern = _require_string(contract, "returned_content_pattern")
    response_contract = contract.get("response_contract")
    if not isinstance(response_contract, dict):
        raise ValueError(f"contract {contract_id}: response_contract must be an object")
    trials = _require_positive_int(contract, "trials")
    minimum_pass_rate = _require_fraction(contract, "minimum_pass_rate")
    trial_receipts = [
        _run_live_trial(
            wrapper,
            workspace_root,
            task_type,
            prompt,
            response_contract,
            _require_string(contract, "output_shape"),
            contract.get("markers"),
            returned_content_pattern,
            expected_model,
            timeout_seconds,
            index,
        )
        for index in range(trials)
    ]
    pass_rate = sum(item["passed"] is True for item in trial_receipts) / trials
    return {
        "contract_id": contract_id,
        "trials": trial_receipts,
        "minimum_pass_rate": minimum_pass_rate,
        "pass_rate": pass_rate,
        "passed": pass_rate >= minimum_pass_rate,
    }


def _run_live_trial(
    wrapper: Path,
    workspace_root: Path,
    task_type: str,
    prompt: str,
    response_contract: dict[str, object],
    output_shape: str,
    markers: object,
    returned_content_pattern: str,
    expected_model: str,
    timeout_seconds: int,
    trial_index: int,
) -> dict[str, object]:
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
        "kafka",
        "--lane",
        "dev",
        "--locus",
        "deployed-lane",
        "--omnibase-path",
        str(workspace_root),
        "--timeout",
        str(timeout_seconds),
    ]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return {
            "trial_index": trial_index,
            "run_id": None,
            "passed": False,
            "failure": (
                "wrapper_nonzero" if completed.returncode != 0 else "wrapper_non_json"
            ),
            "wrapper_exit_code": completed.returncode,
            **_bounded_wrapper_output(completed.stdout, completed.stderr),
        }
    terminal = _find_terminal(payload)
    if terminal is None:
        return {
            "trial_index": trial_index,
            "run_id": None,
            "passed": False,
            "failure": "terminal_evidence_absent",
        }
    refusal = terminal.get("budget_refusal")
    if isinstance(refusal, dict):
        receipt = _predispatch_budget_refusal_receipt(
            terminal, refusal, task_type, trial_index
        )
        receipt["wrapper_exit_code"] = completed.returncode
        return receipt
    if completed.returncode != 0:
        return {
            "trial_index": trial_index,
            "run_id": terminal.get("run_id")
            if isinstance(terminal.get("run_id"), str)
            else None,
            "passed": False,
            "failure": "wrapper_nonzero",
            "wrapper_exit_code": completed.returncode,
            **_bounded_wrapper_output(completed.stdout, completed.stderr),
        }
    evidence = terminal.get("response_contract_evidence")
    budget_evidence = terminal.get("budget_evidence")
    run_id = terminal.get("run_id") or terminal.get("correlation_id")
    if (
        not isinstance(evidence, dict)
        or not isinstance(budget_evidence, dict)
        or not isinstance(run_id, str)
    ):
        return {
            "trial_index": trial_index,
            "run_id": run_id if isinstance(run_id, str) else None,
            "passed": False,
            "failure": "terminal_contract_evidence_absent",
        }
    conveyed = evidence.get("conveyed") is True
    validated = evidence.get("validated") is True
    channel = evidence.get("channel")
    resolved_contract = resolve_task_class_deliverable_contract(
        task_type, response_contract
    )
    resolved_output_shape = resolved_contract.output_shape.value
    resolved_contract_sha256 = canonical_deliverable_contract_sha256(resolved_contract)
    content = terminal.get("response")
    returned_content_valid = isinstance(content, str) and _returned_content_validates(
        content,
        resolved_output_shape,
        response_contract,
        list(resolved_contract.markers),
        returned_content_pattern,
    )
    budget_honoured = _budget_is_honoured(budget_evidence)
    preamble_chars = terminal.get("preamble_chars")
    preamble_evidence_valid = isinstance(preamble_chars, int) and preamble_chars >= 0
    local_model_observed = (
        terminal.get("provider") == "local"
        and terminal.get("model_name") == expected_model
    )
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
    return {
        "trial_index": trial_index,
        "run_id": run_id,
        "conveyed": conveyed,
        "validated": validated,
        "channel": channel,
        "raw_preamble_chars": preamble_chars,
        "budget_honoured": budget_honoured,
        "local_model_observed": local_model_observed,
        "preamble_evidence_valid": preamble_evidence_valid,
        "returned_content_valid": returned_content_valid,
        "output_only": output_only.model_dump(mode="json"),
        "passed": (
            output_only.accepted
            and conveyed
            and validated
            and isinstance(channel, str)
            and bool(channel)
            and output_shape == resolved_output_shape
            and evidence.get("contract_sha256") == resolved_contract_sha256
            and evidence.get("output_shape") == resolved_output_shape
            and budget_honoured
            and local_model_observed
            and preamble_evidence_valid
            and returned_content_valid
            and terminal.get("quality_gate_passed") is True
        ),
    }


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
    return {
        "trial_index": trial_index,
        "run_id": run_id if isinstance(run_id, str) else None,
        "passed": False,
        "failure": (
            "predispatch_budget_refusal" if valid_refusal else "invalid_budget_refusal"
        ),
        "budget_refusal": refusal,
    }


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
