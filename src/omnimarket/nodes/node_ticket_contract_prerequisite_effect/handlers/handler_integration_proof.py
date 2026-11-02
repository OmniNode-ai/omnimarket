# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A runtime ticket's definition of done carries an integration proof.

Operator ruling 2026-11-01T14:45:51Z: work is verified only when the full
integration is done, deployed on the runtime and tested with real dependencies.
A pull request that changes something that runs owes its ticket contract one
``dod_evidence`` item whose id names an integration proof, whose execution scope
is the local Done gate (the hosted verifier cannot reach a runtime), and whose
checks include a command that exercises the change on a live surface. A change
that only touches docs, tests, contracts, workflows or scripts owes none.

Pure: no I/O. The git read that supplies ``changed_paths`` belongs to
``HandlerTicketContractPrerequisite``.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any, Final, cast

import yaml
from omnibase_core.utils.util_safe_yaml_loader import load_yaml_mapping_no_duplicates

from omnimarket.enums.enum_check_proof_class import EnumCheckProofClass
from omnimarket.nodes.node_dod_verify.services.check_proof_class import (
    classify_command,
    command_heads,
)
from omnimarket.nodes.node_ticket_contract_prerequisite_effect.models.model_integration_proof_request import (
    ModelIntegrationProofRequest,
)
from omnimarket.nodes.node_ticket_contract_prerequisite_effect.models.model_integration_proof_result import (
    ModelIntegrationProofResult,
)

INTEGRATION_PROOF_ID_MARKER: Final[str] = "integration-proof"
INTEGRATION_PROOF_SCOPE: Final[str] = "local_done_gate"

# Roots whose files are built into, or deploy, the runtime. A path outside
# these is not a runtime change; an unlisted file under src/ counts as one
# (unknown fails closed as runtime).
_RUNTIME_ROOTS: Final[tuple[str, ...]] = ("src/", "docker/", "deploy/")
_RUNTIME_FILE_PREFIXES: Final[tuple[str, ...]] = ("Dockerfile",)
_NON_RUNTIME_SEGMENTS: Final[frozenset[str]] = frozenset({"tests", "test", "docs"})
_NON_RUNTIME_SUFFIXES: Final[frozenset[str]] = frozenset({".md", ".rst", ".txt"})

# The head that runs the change itself on a runtime: the ONEX CLI (``onex
# run-node`` executes the node on the deployed runtime over Kafka, ``onex
# lab-work`` places a command on a lab host with its real dependencies).
_LIVE_RUNNER_HEADS: Final[frozenset[str]] = frozenset({"onex"})
_PROOF_CHECK_TYPES: Final[frozenset[str]] = frozenset({"command", "test_passes"})

_EXAMPLE_LIMIT: Final[int] = 3


def runtime_paths(paths: tuple[str, ...]) -> tuple[str, ...]:
    """The changed paths that run: runtime roots and Dockerfiles, minus docs and tests."""
    hits = []
    for path in paths:
        pure = PurePosixPath(path)
        runs = path.startswith(_RUNTIME_ROOTS) or pure.name.startswith(
            _RUNTIME_FILE_PREFIXES
        )
        if not runs:
            continue
        if _NON_RUNTIME_SEGMENTS & set(pure.parts[:-1]):
            continue
        if pure.suffix in _NON_RUNTIME_SUFFIXES:
            continue
        hits.append(path)
    return tuple(hits)


def _check_value(check: dict[str, Any]) -> str:
    value = check.get("command") or check.get("check_value") or ""
    return value if isinstance(value, str) else ""


def _is_live_probe(check: object) -> bool:
    """A command that runs the change on a live surface and can fail."""
    if not isinstance(check, dict) or check.get("check_type") not in _PROOF_CHECK_TYPES:
        return False
    command = _check_value(cast("dict[str, Any]", check))
    if not command.strip():
        return False
    proof = classify_command(command)
    if proof is EnumCheckProofClass.READBACK:
        return True
    return proof is EnumCheckProofClass.BEHAVIOR and bool(
        _LIVE_RUNNER_HEADS & set(command_heads(command))
    )


def _proof_items(items: list[Any]) -> tuple[list[str], list[str]]:
    """Ids of well-formed integration-proof items, and the ids named but malformed."""
    proofs: list[str] = []
    malformed: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("id", ""))
        if INTEGRATION_PROOF_ID_MARKER not in item_id.lower():
            continue
        scope = item.get("execution_scope")
        checks = item.get("checks")
        if (
            scope == INTEGRATION_PROOF_SCOPE
            and isinstance(checks, list)
            and any(_is_live_probe(check) for check in checks)
        ):
            proofs.append(item_id)
        else:
            malformed.append(item_id)
    return proofs, malformed


def _refusal(ticket_id: str, hits: tuple[str, ...], malformed: list[str]) -> str:
    shown = ", ".join(hits[:_EXAMPLE_LIMIT])
    more = (
        f" (+{len(hits) - _EXAMPLE_LIMIT} more)" if len(hits) > _EXAMPLE_LIMIT else ""
    )
    named = f" Named but not accepted: {', '.join(malformed)}." if malformed else ""
    return (
        f"contracts/{ticket_id}.yaml: this change runs ({len(hits)} runtime "
        f"path(s): {shown}{more}) so its definition of done needs an integration "
        f"proof: a dod_evidence item whose id contains "
        f"'{INTEGRATION_PROOF_ID_MARKER}', execution_scope: {INTEGRATION_PROOF_SCOPE}, "
        "and a command or test_passes check that runs the change on a live "
        "surface (an `onex` command such as `onex run-node` or `onex lab-work`, "
        f"or an asserted read of a live service).{named}"
    )


def check_integration_proof(
    request: ModelIntegrationProofRequest,
) -> ModelIntegrationProofResult:
    """Refuse a runtime ticket whose contract carries no integration proof."""
    hits = runtime_paths(request.changed_paths)
    if not hits:
        return ModelIntegrationProofResult()
    try:
        raw = load_yaml_mapping_no_duplicates(
            request.contract_text, source=f"contracts/{request.ticket_id}.yaml"
        )
    except (yaml.YAMLError, ValueError) as exc:
        return ModelIntegrationProofResult(
            runtime_paths=hits,
            failure=f"contracts/{request.ticket_id}.yaml: {exc}",
        )
    items = raw.get("dod_evidence")
    proofs, malformed = _proof_items(items if isinstance(items, list) else [])
    if proofs:
        return ModelIntegrationProofResult(
            runtime_paths=hits, proof_item_ids=tuple(proofs)
        )
    return ModelIntegrationProofResult(
        runtime_paths=hits,
        failure=_refusal(request.ticket_id, hits, malformed),
    )


class HandlerIntegrationProof:
    """The node's handler for the integration-proof check."""

    def handle(
        self, request: ModelIntegrationProofRequest
    ) -> ModelIntegrationProofResult:
        return check_integration_proof(request)


__all__ = [
    "INTEGRATION_PROOF_ID_MARKER",
    "INTEGRATION_PROOF_SCOPE",
    "HandlerIntegrationProof",
    "check_integration_proof",
    "runtime_paths",
]
