# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared admission decisions and gate evidence consumed by exemplar nodes."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

NodeArchetype = Literal["compute", "effect", "orchestrator", "reducer"]
ViolationCode = Literal[
    "gate_absent_in_repository",
    "in_baseline",
    "no_bindings",
    "gate_failed",
    "binding_non_canonical",
    "handler_source_missing",
    "handler_unparseable",
    "handle_not_found",
    "handle_kwargs",
    "handle_varargs",
    "handle_zeroarg",
    "handle_multi_positional",
    "handle_keyword_only",
    "handle_param_untyped",
    "handle_return_untyped",
    "impure_import",
    "import_source_missing",
    "deny_secret_literal",
    "deny_eval_exec",
    "deny_pickle",
    "contract_invalid",
    "unit_test_dir_missing",
    "test_receipt_missing",
    "receipt_no_required_checks",
    "required_check_missing",
    "receipt_run_other_commit",
    "required_check_not_success",
    "node_tests_not_collected",
]


class ModelAdmissionViolation(BaseModel):
    """One refusal reason; details never contain secret values."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: ViolationCode
    detail: str = ""


class ModelGateReceipt(BaseModel):
    """Evidence attached only to admitted candidates."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    gate: str
    gate_version: str
    baseline_sha256: str
    result: Literal["pass"]
    commit: str
    check_run_ids: list[int]


class ModelAdmissionDecision(BaseModel):
    """All violations or an admission with its archetype rank and receipt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    node_id: str
    node_archetype: NodeArchetype
    contract_path: str
    handler_module: str
    admitted: bool
    violations: list[ModelAdmissionViolation]
    coverage_state: Literal["known", "unknown"]
    coverage_percent: float | None
    handler_lines: int
    rank: int | None
    receipt: ModelGateReceipt | None


class ModelExemplarAdmissionResult(BaseModel):
    """Decisions in input order and admitted nodes in archetype rank order."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: str
    commit: str
    gate: str
    gate_version: str
    rules_version: str
    pure_import_list_version: str
    decisions: list[ModelAdmissionDecision]
    admitted_by_archetype: dict[str, list[str]]
    admitted_count: int
    refused_count: int
