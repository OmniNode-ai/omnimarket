# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Which lane a developer's hook events belong to, and whether the developer may use it (OMN-21599).

Pure: ``admit(request) -> decision`` over plain mappings, no I/O. The prompt hook of the
omniclaude plugin used to select the hook-edge contract's declared lane for every developer. A
collaborator bound to another lane (``onex profile bind-lane <lane>`` writes
``developer.lane_binding`` into ``~/.onex/config.yaml``) was then either checked against the
wrong lane, with no login there, or passed unchecked. This node decides it from facts the hook
reads and hands over; it names no lane. Every lane name arrives in the request, from the
hook-edge contract (``declared_lane``, ``barred_lanes``, the lanes) and from the private overlay
declarations (each lane's own list of declared people).

The decision:

1. The selected lane is the developer's bound lane, else the declared lane. A binding that is
   not a lane name, or names a lane the contract does not know, is refused.
2. A declaration that is present but malformed is refused for the selected lane.
3. A lane with no declaration installed keeps the operator behaviour (nothing checked), unless
   the developer is bound to it: a bound lane whose list cannot be read cannot vouch for anyone,
   so the developer is refused.
4. The developer's principal is read from the lane store when the lane's credential source is
   the lane store, from the operator env file when it is that, and from the lane store for a bound
   lane that has no credential source. No principal is a refusal, never a pass.
5. A principal on the selected lane's own list is a declared collaborator (admitted; the hook
   drops the held event classes). A principal on another lane's list is refused: a person derives
   nothing from a lane they are not declared on. A principal on a list while the selected lane is
   one a collaborator never uses (``barred_lanes``) is refused. Any other principal is the
   operator's own, unchanged.

The hook loads this file BY PATH (no package import runs on the per-prompt path), so the module
level imports the standard library only; the node's pydantic models are imported inside
:meth:`HandlerDeveloperLaneAdmission.handle`, which only the runtime calls. The constants below
are the vocabulary ``contract.yaml`` and the models repeat; a test pins them together.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

PROFILE_DEVELOPER_BLOCK = "developer"
PROFILE_LANE_BINDING_KEY = "lane_binding"
LANE_STORE_LANES_BLOCK = "lanes"
LANE_STORE_USERNAME_KEY = "sasl_username"
DECLARATION_SCHEMA_VERSION = "developer_principal.v1"

SOURCE_LANE_STORE = "lane_store"
SOURCE_OPERATOR_ENV = "operator_env"
SOURCE_NONE = "none"

VERDICT_OPERATOR = "operator"
VERDICT_COLLABORATOR = "collaborator"
VERDICT_REFUSED = "refused"

FAULT_CONFIG = "config"
FAULT_CREDENTIAL = "credential"

REFUSAL_BINDING_INVALID = "binding-invalid"
REFUSAL_BINDING_UNKNOWN_LANE = "binding-unknown-lane"
REFUSAL_DECLARATION_MALFORMED = "declaration-malformed"
REFUSAL_DECLARATION_MISSING = "bound-lane-declaration-missing"
REFUSAL_NO_PRINCIPAL = "no-declared-principal"
REFUSAL_BARRED_LANE = "collaborator-on-barred-lane"
REFUSAL_OTHER_LANE = "principal-declared-for-another-lane"

_REFUSAL_FAULT = {
    REFUSAL_BINDING_INVALID: FAULT_CONFIG,
    REFUSAL_BINDING_UNKNOWN_LANE: FAULT_CONFIG,
    REFUSAL_DECLARATION_MALFORMED: FAULT_CONFIG,
    REFUSAL_DECLARATION_MISSING: FAULT_CREDENTIAL,
    REFUSAL_NO_PRINCIPAL: FAULT_CREDENTIAL,
    REFUSAL_BARRED_LANE: FAULT_CREDENTIAL,
    REFUSAL_OTHER_LANE: FAULT_CREDENTIAL,
}


def _decision(
    lane: str, verdict: str, refusal: str | None = None, detail: str = ""
) -> dict[str, Any]:
    return {
        "selected_lane": lane,
        "verdict": verdict,
        "refusal": refusal,
        "fault": _REFUSAL_FAULT[refusal] if refusal else None,
        "detail": detail,
    }


def _refuse(lane: str, refusal: str, detail: str) -> dict[str, Any]:
    return _decision(lane, VERDICT_REFUSED, refusal, detail)


def _declared_principals(declaration: Mapping[str, Any]) -> frozenset[str] | None:
    """The principals a well-formed declaration lists, or ``None`` when it is malformed."""
    rows = declaration.get("principals")
    if (
        declaration.get("schema_version") != DECLARATION_SCHEMA_VERSION
        or not isinstance(rows, Sequence)
        or isinstance(rows, str)
        or not rows
    ):
        return None
    names: set[str] = set()
    for row in rows:
        name = row.get("principal") if isinstance(row, Mapping) else None
        if not isinstance(name, str) or not name.strip():
            return None
        names.add(name)
    return frozenset(names)


def _binding(store: Mapping[str, Any] | None) -> tuple[str | None, bool]:
    """``(lane, valid)``: the bound lane name, or ``None``; ``valid`` is False for a bad value."""
    developer = (store or {}).get(PROFILE_DEVELOPER_BLOCK)
    if developer is None:
        return None, True
    if not isinstance(developer, Mapping):
        return None, False
    value = developer.get(PROFILE_LANE_BINDING_KEY)
    if value is None:
        return None, True
    if not isinstance(value, str) or not value.strip():
        return None, False
    return value.strip(), True


def _principal(
    store: Mapping[str, Any] | None, lane: str, facts: Mapping[str, Any], bound: bool
) -> str | None:
    source = facts.get("credential_source", SOURCE_NONE)
    if source == SOURCE_OPERATOR_ENV:
        value = facts.get("env_principal")
        return value if isinstance(value, str) and value.strip() else None
    if source != SOURCE_LANE_STORE and not bound:
        return None
    lanes = (store or {}).get(LANE_STORE_LANES_BLOCK)
    entry = lanes.get(lane) if isinstance(lanes, Mapping) else None
    value = entry.get(LANE_STORE_USERNAME_KEY) if isinstance(entry, Mapping) else None
    # The broker identity is the stripped name, as the lane store login writes it.
    return value.strip() if isinstance(value, str) and value.strip() else None


def admit(request: Mapping[str, Any]) -> dict[str, Any]:
    """Decide the selected lane and the developer's standing on it from the facts in ``request``."""
    declared = request["declared_lane"]
    lanes: Mapping[str, Mapping[str, Any]] = request["lanes"]
    store = request.get("lane_store")
    binding, valid = _binding(store)
    if not valid:
        return _refuse(
            declared,
            REFUSAL_BINDING_INVALID,
            f"{PROFILE_DEVELOPER_BLOCK}.{PROFILE_LANE_BINDING_KEY} must name a lane",
        )
    if binding is not None and binding not in lanes:
        return _refuse(
            declared,
            REFUSAL_BINDING_UNKNOWN_LANE,
            f"{PROFILE_DEVELOPER_BLOCK}.{PROFILE_LANE_BINDING_KEY} names {binding!r}, "
            f"which the hook edge does not know (known lanes {sorted(lanes)})",
        )
    selected = binding or declared
    bound = selected != declared
    facts = lanes[selected]
    own = facts.get("declaration")
    listed: dict[str, frozenset[str]] = {}
    for name, entry in lanes.items():
        declaration = entry.get("declaration")
        if declaration is None:
            continue
        principals = _declared_principals(declaration)
        if principals is None and name == selected:
            return _refuse(
                selected,
                REFUSAL_DECLARATION_MALFORMED,
                f"the principal declaration of lane {selected!r} is malformed",
            )
        if principals is not None:
            listed[name] = principals
    if own is None:
        if not bound:
            return _decision(selected, VERDICT_OPERATOR)
        return _refuse(
            selected,
            REFUSAL_DECLARATION_MISSING,
            f"bound lane {selected!r} has no principal declaration installed to check against",
        )
    principal = _principal(store, selected, facts, bound)
    if principal is None:
        return _refuse(
            selected, REFUSAL_NO_PRINCIPAL, "selected lane has no declared principal"
        )
    if principal in listed.get(selected, frozenset()):
        if selected in request.get("barred_lanes", ()):
            return _refuse(
                selected,
                REFUSAL_BARRED_LANE,
                f"a declared collaborator never uses lane {selected!r}; "
                "bind the lane the collaborator belongs to with 'onex profile bind-lane'",
            )
        return _decision(selected, VERDICT_COLLABORATOR)
    if any(principal in names for names in listed.values()):
        return _refuse(
            selected,
            REFUSAL_OTHER_LANE,
            f"the principal is declared on another lane, not on {selected!r}",
        )
    return _decision(selected, VERDICT_OPERATOR)


class HandlerDeveloperLaneAdmission:
    """Runtime entry: the typed request in, the typed decision out."""

    def handle(self, request: Any) -> Any:
        from omnimarket.nodes.node_developer_lane_admission_compute.models.model_developer_lane_admission import (
            ModelDeveloperLaneAdmissionResult,
        )

        return ModelDeveloperLaneAdmissionResult(**admit(request.model_dump()))


__all__ = ["HandlerDeveloperLaneAdmission", "admit"]
