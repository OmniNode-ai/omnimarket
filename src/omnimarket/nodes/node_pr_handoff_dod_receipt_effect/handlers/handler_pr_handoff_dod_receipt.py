# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read canonical DoD receipts and judge their ticket and exact head binding."""

from __future__ import annotations

import os
import re
from pathlib import Path

import yaml
from omnibase_core.models.contracts.ticket.model_dod_receipt import ModelDodReceipt

from omnimarket.config.state_root import (
    OnexStateRootUnconfiguredError,
    resolve_onex_state_root,
)
from omnimarket.handlers.runtime_paths import runtime_path
from omnimarket.nodes.contract_topics import contract_section
from omnimarket.nodes.node_pr_handoff_dod_receipt_effect.models.model_pr_handoff_dod_receipt_policy import (
    ModelPrHandoffDodReceiptPolicy,
)
from omnimarket.nodes.node_pr_handoff_dod_receipt_effect.models.model_pr_handoff_dod_receipt_request import (
    ModelPrHandoffDodReceiptRequest,
)
from omnimarket.nodes.node_pr_handoff_dod_receipt_effect.models.model_pr_handoff_dod_receipt_result import (
    ModelPrHandoffDodReceiptResult,
)

_CONTRACT = Path(__file__).resolve().parents[1] / "contract.yaml"


class HandlerPrHandoffDodReceipt:
    """A missing, failed or unreadable receipt never admits a handoff."""

    def __init__(self, *, contract_path: Path = _CONTRACT) -> None:
        self._policy = ModelPrHandoffDodReceiptPolicy.model_validate(
            contract_section(contract_path, "receipt_policy")
        )

    def _root(self, request: ModelPrHandoffDodReceiptRequest) -> Path:
        policy = self._policy
        if request.receipts_root is not None:
            if not request.receipts_root.strip():
                raise ValueError("receipts_root overlay is empty")
            return Path(request.receipts_root)
        for key in (policy.receipt_root_env, policy.evidence_root_env):
            if value := os.environ.get(key):
                return Path(value)
        return resolve_onex_state_root(purpose="handoff DoD receipts") / runtime_path(
            policy.state_home_key
        )

    def _current_receipts(self, base: Path) -> tuple[Path, ...]:
        """Select the highest numeric supersession for each evidence item/check."""
        try:
            base.stat()
        except FileNotFoundError:
            return ()
        selected: dict[tuple[str, str], tuple[int, Path]] = {}
        paths: list[Path] = []
        for folder in sorted(base.iterdir()):
            if folder.name == self._policy.summary_name and folder.is_file():
                paths.append(folder)
            elif folder.is_dir():
                for path in sorted(folder.iterdir()):
                    match = re.fullmatch(self._policy.receipt_pattern, path.name)
                    if match is None or not path.is_file():
                        continue
                    key = (folder.name, match.group(1))
                    rank = int(match.group(2)) if match.group(2) else -1
                    previous = selected.get(key)
                    if previous is not None and rank == previous[0]:
                        raise ValueError(
                            f"ambiguous receipt revisions: {previous[1]} and {path}"
                        )
                    if previous is None or rank > previous[0]:
                        selected[key] = (rank, path)
        paths.extend(path for _, path in selected.values())
        return tuple(sorted(paths))

    def handle(
        self, request: ModelPrHandoffDodReceiptRequest
    ) -> ModelPrHandoffDodReceiptResult:
        """Validate the core receipt schema, then require exact ticket/head/PASS."""
        policy = self._policy
        root = ""
        receipt_path: Path | None = None
        passing: list[str] = []
        failing: list[str] = []

        def result(code: int, line: str) -> ModelPrHandoffDodReceiptResult:
            admitted = code == policy.admitted_code
            return ModelPrHandoffDodReceiptResult(
                correlation_id=request.correlation_id,
                ticket=request.ticket,
                head=request.head,
                status="success" if admitted else "failure",
                admitted=admitted,
                code=code,
                line=line,
                root=root,
                passing=tuple(passing),
                failing=tuple(failing),
            )

        if not re.fullmatch(policy.ticket_pattern, request.ticket) or not re.fullmatch(
            policy.head_pattern, request.head
        ):
            return result(
                policy.refused_code, "ticket and full immutable PR head are required"
            )
        try:
            root_path = self._root(request)
            root = str(root_path)
            base = root_path / request.ticket
            for path in self._current_receipts(base):
                receipt_path = path
                receipt = ModelDodReceipt.model_validate(
                    yaml.safe_load(path.read_text(encoding="utf-8"))
                )
                if (
                    receipt.ticket_id != request.ticket
                    or receipt.commit_sha != request.head
                ):
                    continue
                name = str(path.relative_to(base))
                if receipt.status.value == policy.passing_status:
                    passing.append(name)
                else:
                    failing.append(f"{name}={receipt.status.value}")
        except (
            OSError,
            ValueError,
            OnexStateRootUnconfiguredError,
            yaml.YAMLError,
        ) as exc:
            return result(
                policy.unreadable_code,
                f"unreadable dod_verify receipt at {receipt_path or root or 'unconfigured root'} "
                f"({type(exc).__name__})",
            )
        if failing:
            return result(
                policy.refused_code,
                "non-passing receipts at PR head: " + ", ".join(failing),
            )
        if not passing:
            return result(
                policy.refused_code,
                f"no passing dod_verify receipt for {request.ticket} at head {request.head}",
            )
        return result(
            policy.admitted_code,
            f"dod-receipt {request.ticket} head={request.head} PASS",
        )
