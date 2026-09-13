# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2026 OmniNode Team
"""Contract-declared identifier grounding for the delegation quality gate.

OMN-18297. Every check the gate ran before this one was response-only: refusal,
emptiness, length, marker presence, semantic adequacy. None of them could see
that a response cited identifiers that appear nowhere in the input it was asked
to summarise, so local delegation
``d715f096-27b9-444f-9355-3554819ef8a5`` -- a 22-row standup over ~18K input
tokens -- shipped eight pull-request citations absent from the fed rows and the
gate scored it ``passed=true score=1.0``.

The identifier classes, the lookup form for each, the markers a response may
use to declare an identifier unverified, and the failure policy are all read
from the ``identifier_grounding`` block of this node's ``contract.yaml``. This
module compiles that declaration and applies it; it declares no class and no
pattern of its own. A malformed or absent block is a configuration error, not a
silent pass -- see :func:`resolve_identifier_grounding_policy`.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

import yaml
from omnibase_infra.errors import ProtocolConfigurationError

from omnimarket.inference.delegation_config_provenance import resolve_path_config
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_identifier_grounding import (
    ModelIdentifierClass,
    ModelIdentifierGroundingPolicy,
    ModelIdentifierGroundingVerdict,
    ModelUngroundedIdentifier,
)

_CONTRACT_PATH_CONFIG_KEY = "QUALITY_GATE_CONTRACT_PATH"
# The gate node's own contract. This module lives OUTSIDE the node package on
# purpose: reading a file is I/O, and the node-purity gate (OMN-13283) forbids
# I/O inside a node. The same separation OMN-15628 made for routing_tiers.yaml
# -- the canonical path and its env-pinned resolver live in a shared non-node
# module -- rather than annotating a suppression onto a node.
_DEFAULT_CONTRACT_PATH = (
    Path(__file__).parent.parent
    / "nodes"
    / "node_delegation_quality_gate_reducer"
    / "contract.yaml"
)
_CONTRACT_SECTION = "identifier_grounding"

# Well-formed <think>...</think> pairs are removed before the stray-terminator
# rule runs, so a model that emits a correctly paired trace does not leave a
# terminator behind for the answer-segment split to trip over.
_PAIRED_TRACE_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


@lru_cache(maxsize=1)
def resolve_identifier_grounding_policy() -> ModelIdentifierGroundingPolicy:
    """Load and validate the contract's ``identifier_grounding`` declaration.

    Fail-closed. An absent contract file, an absent section, unparseable YAML,
    or a declaration that does not validate raises
    :class:`ProtocolConfigurationError` rather than degrading to "no classes",
    which would present an unevaluated check as a clean one.
    """
    contract_path, _ = resolve_path_config(
        _CONTRACT_PATH_CONFIG_KEY,
        _DEFAULT_CONTRACT_PATH,
    )
    if not contract_path.exists():
        raise ProtocolConfigurationError(
            f"quality gate contract not found at {contract_path}; "
            f"{_CONTRACT_SECTION} policy cannot be resolved"
        )
    try:
        raw = yaml.safe_load(contract_path.read_text())
    except yaml.YAMLError as exc:  # pragma: no cover - defensive parse guard
        raise ProtocolConfigurationError(
            f"quality gate contract at {contract_path} is not valid YAML: {exc}"
        ) from exc
    if not isinstance(raw, dict) or not isinstance(raw.get(_CONTRACT_SECTION), dict):
        raise ProtocolConfigurationError(
            f"quality gate contract at {contract_path} declares no "
            f"{_CONTRACT_SECTION} section"
        )
    return ModelIdentifierGroundingPolicy.model_validate(raw[_CONTRACT_SECTION])


@lru_cache(maxsize=64)
def _compiled(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern)


def answer_segment(content: str, *, terminator: str) -> str:
    """Return the answer, isolated from a leaked reasoning trace.

    OMN-18278 (open, deliberately NOT fixed here): local-tier responses ship the
    model's scratchpad ahead of the answer, closed by a terminator with no
    matching opening tag, which the ordinary paired-tag strip leaves untouched.
    Grading grounding over the scratchpad would grade the wrong text -- a
    scratchpad enumerates candidate identifiers the model then discards. This
    isolates the answer for THIS check only; the response the caller receives is
    unchanged, and removing the trace from it stays OMN-18278's work.
    """
    stripped = _PAIRED_TRACE_RE.sub("", content)
    index = stripped.rfind(terminator)
    if index == -1:
        return stripped
    return stripped[index + len(terminator) :]


def _is_marked_unverified(
    content: str,
    *,
    end: int,
    policy: ModelIdentifierGroundingPolicy,
) -> bool:
    """Whether the response declares this occurrence unverified."""
    window = content[end : end + policy.unverified_window]
    return any(_compiled(marker).search(window) for marker in policy.unverified_markers)


def _is_grounded(
    *,
    identifier_class: ModelIdentifierClass,
    token: str,
    source: str,
) -> bool:
    looked_up = identifier_class.grounding_form.format(token=token)
    if looked_up in source:
        return True
    if not identifier_class.prefix_match:
        return False
    # An abbreviated sha is grounded by a longer source occurrence it prefixes.
    return any(
        candidate.startswith(token)
        for candidate in _compiled(identifier_class.pattern).findall(source)
    )


def evaluate_identifier_grounding(
    *,
    content: str,
    grounding_source: str | None,
    policy: ModelIdentifierGroundingPolicy,
) -> ModelIdentifierGroundingVerdict:
    """Check every declared identifier class in ``content`` against the source.

    ``grounding_source`` is the delegated prompt. ``None`` -- the bus path,
    whose ``ModelQualityGateIntent`` carries no prompt -- yields an
    unevaluated verdict, which the caller records as a skipped check and
    excludes from the scored total. It is never reported as a pass.
    """
    if grounding_source is None:
        return ModelIdentifierGroundingVerdict(evaluated=False)

    segment = answer_segment(
        content, terminator=policy.answer_segment.stray_trace_terminator
    )
    checked = 0
    ungrounded: list[ModelUngroundedIdentifier] = []
    seen: set[tuple[str, str]] = set()

    for identifier_class in policy.classes:
        for match in _compiled(identifier_class.pattern).finditer(segment):
            token = match.group("token")
            checked += 1
            key = (identifier_class.class_name, token)
            if key in seen:
                continue
            if _is_grounded(
                identifier_class=identifier_class,
                token=token,
                source=grounding_source,
            ):
                continue
            if _is_marked_unverified(segment, end=match.end(), policy=policy):
                continue
            seen.add(key)
            ungrounded.append(
                ModelUngroundedIdentifier(
                    class_name=identifier_class.class_name,
                    identifier=match.group(0).strip(),
                    looked_up_as=identifier_class.grounding_form.format(token=token),
                )
            )

    return ModelIdentifierGroundingVerdict(
        evaluated=True,
        checked_count=checked,
        ungrounded=tuple(ungrounded[: policy.failure_policy.max_reported]),
    )


__all__: list[str] = [
    "answer_segment",
    "evaluate_identifier_grounding",
    "resolve_identifier_grounding_policy",
]
