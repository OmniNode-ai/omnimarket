# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure fold: a delegation terminal's cohort key onto its delegation_events row.

OMN-18930, K3 of OMN-18925. The plan's K3 row requires that "each compared
run's row carries its complete key, and the two rows differ only where the keys
differ". The key is stamped on the terminal by the consumer that ran the
delegation (its typed shape is omnibase_infra's ``ModelDelegationCohortKey``,
omnibase_infra#4054); this fold reads it off the terminal and returns the three
row columns. The effect writers (``HandlerProjectionDelegation`` and
``DelegationProjectionRunner``) persist what it returns and decide nothing.

The rules:

* **Never a dimension the terminal did not carry.** Nothing here is read from
  the projection's own process, clock or configuration. The writer runs in its
  own container, so its build is not the build that ran the delegation.
* **Complete or refused.** A key missing a dimension, carrying an unknown one,
  or holding a malformed build identity is refused by name into
  ``cohort_key_refusal``, and no key or digest is stored. An incomplete key is
  not a smaller key; comparing on it is the offset-489 error.
* **Absent is not refused.** A terminal that carried no key yields no column at
  all, so a keyless re-emit for the same correlation leaves a stored key alone.
* **The digest is the infra model's.** ``cohort_key_sha256`` is SHA-256 over
  the key serialized with sorted members and compact separators, which is
  ``ModelDelegationCohortKey.key_sha256`` for a key the producer serialized with
  ``model_dump(mode="json")``. The unit tests pin this against the real dev-lane
  captures.

Only the top-level dimensions and the build identity are checked in depth here:
the build identity is the dimension K3 exists to separate, and the full nested
validation belongs to the typed infra model, which this repository cannot
import until an omnibase_infra release carries it.
"""

from __future__ import annotations

import hashlib
import json
import re

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from omnimarket.models.delegation.wire.model_delegate_skill_terminal_projection import (
    ModelDelegateSkillTerminalProjection,
)

#: The ten dimensions of ``ModelDelegationCohortKey``, in its field order.
DELEGATION_COHORT_KEY_DIMENSIONS: tuple[str, ...] = (
    "prompt_sha256",
    "resolved_task_type",
    "response_contract_sha256",
    "lane",
    "build_identity",
    "consumer_identity",
    "first_hop_identity",
    "provider_policy",
    "deadline_seconds",
    "retry_bounds",
)

_OBJECT_DIMENSIONS: tuple[str, ...] = (
    "build_identity",
    "consumer_identity",
    "first_hop_identity",
    "provider_policy",
    "retry_bounds",
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_IMAGE_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


class ModelDelegationCohortKeyFold(BaseModel):
    """The three delegation_events cohort-key columns one terminal yields."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    cohort_key: dict[str, JsonValue] | None = Field(default=None)
    cohort_key_sha256: str | None = Field(default=None)
    cohort_key_refusal: str | None = Field(default=None)
    #: False when the terminal carried no key; the writer then names no column.
    carried: bool = Field(default=False)

    @model_validator(mode="after")
    def _one_outcome(self) -> ModelDelegationCohortKeyFold:
        has_key = self.cohort_key is not None
        if has_key != (self.cohort_key_sha256 is not None):
            raise ValueError("cohort_key and cohort_key_sha256 are set together")
        if self.cohort_key_sha256 is not None and not _SHA256.fullmatch(
            self.cohort_key_sha256
        ):
            raise ValueError("cohort_key_sha256 must be lowercase SHA-256 hex")
        if has_key and self.cohort_key_refusal is not None:
            raise ValueError("a stored cohort key cannot also carry a refusal")
        if self.carried != (has_key or self.cohort_key_refusal is not None):
            raise ValueError("a carried key is either stored or refused")
        return self

    def row_columns(self) -> dict[str, object]:
        """The columns to name on the row; none when the terminal had no key."""
        if not self.carried:
            return {}
        return {
            "cohort_key": self.cohort_key,
            "cohort_key_sha256": self.cohort_key_sha256,
            "cohort_key_refusal": self.cohort_key_refusal,
        }


def cohort_key_sha256(key: dict[str, JsonValue]) -> str:
    """SHA-256 of the key with sorted members and compact separators."""
    canonical = json.dumps(key, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _is_trimmed_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and value == value.strip()


def _refusal(key: dict[str, JsonValue]) -> str | None:
    """Name the first reason the key is not complete, or None when it is."""
    missing = [name for name in DELEGATION_COHORT_KEY_DIMENSIONS if name not in key]
    if missing:
        return f"missing dimensions: {', '.join(missing)}"
    unknown = sorted(set(key) - set(DELEGATION_COHORT_KEY_DIMENSIONS))
    if unknown:
        return f"unknown dimensions: {', '.join(unknown)}"
    for name in _OBJECT_DIMENSIONS:
        value = key[name]
        if not isinstance(value, dict):
            return f"{name} must be an object"
        if not value:
            return f"{name} must be a nonempty object"
    if not isinstance(key["prompt_sha256"], str) or not _SHA256.fullmatch(
        key["prompt_sha256"]
    ):
        return "prompt_sha256 must be lowercase SHA-256 hex"
    contract = key["response_contract_sha256"]
    if contract is not None and (
        not isinstance(contract, str) or not _SHA256.fullmatch(contract)
    ):
        return "response_contract_sha256 must be lowercase SHA-256 hex or null"
    for name in ("resolved_task_type", "lane"):
        if not _is_trimmed_text(key[name]):
            return f"{name} must be a nonblank trimmed string"
    deadline = key["deadline_seconds"]
    if (
        isinstance(deadline, bool)
        or not isinstance(deadline, (int, float))
        or deadline <= 0
    ):
        return "deadline_seconds must be a positive number"
    build = key["build_identity"]
    if not isinstance(build, dict):  # already refused above; narrows the type
        return "build_identity must be an object"
    for field, pattern, shape in (
        ("source_revision", _GIT_SHA, "a full lowercase git sha"),
        ("image_digest", _IMAGE_DIGEST, "sha256:<64 lowercase hex>"),
        ("build_provenance_sha256", _SHA256, "lowercase SHA-256 hex"),
    ):
        value = build.get(field)
        if not isinstance(value, str) or not pattern.fullmatch(value):
            return f"build_identity.{field} must be {shape}"
    return None


class HandlerDelegationCohortKeyFold:
    """COMPUTE: terminal in, the row's cohort-key columns out. No I/O."""

    def handle(
        self, request: ModelDelegateSkillTerminalProjection
    ) -> ModelDelegationCohortKeyFold:
        key = request.cohort_key
        if key is None:
            return ModelDelegationCohortKeyFold()
        if not isinstance(key, dict):
            return ModelDelegationCohortKeyFold(
                cohort_key_refusal="cohort_key must be a JSON object", carried=True
            )
        refusal = _refusal(key)
        if refusal is not None:
            return ModelDelegationCohortKeyFold(
                cohort_key_refusal=refusal, carried=True
            )
        return ModelDelegationCohortKeyFold(
            cohort_key=key, cohort_key_sha256=cohort_key_sha256(key), carried=True
        )


__all__ = [
    "DELEGATION_COHORT_KEY_DIMENSIONS",
    "HandlerDelegationCohortKeyFold",
    "ModelDelegationCohortKeyFold",
    "cohort_key_sha256",
]
