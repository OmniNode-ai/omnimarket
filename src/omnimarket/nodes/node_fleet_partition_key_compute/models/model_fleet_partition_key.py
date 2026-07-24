# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Fleet topology partition/keying models (OMN-14978).

Encodes the keying half of the topology decision spec'd in
``docs/plans/2026-07-23-distributed-validation-context-aware-runtime-plan.md``
§2 D-topology: "Converting to real parallel capacity requires a
partition/keying design (key by repo+branch so no two hosts ever hold the
same branch concurrently)."

Key construction: ``f"{repo}:{branch}"``. The ``:`` delimiter is
collision-safe by construction — the repo slug pattern
(``^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$``, matching
``ModelPushValidationRequest.repo``) never contains ``:``, and git's own ref
grammar forbids ``:`` in a branch name (``git check-ref-format``). The join
is therefore injective: two distinct ``(repo, branch)`` pairs can never
produce the same key, and a single ``(repo, branch)`` pair always produces
the same key regardless of partition count — the identity is independent of
topology size so a later re-partition does not change WHICH logical stream a
key belongs to (only, per Kafka's own partitioner, which physical partition
that stream currently hashes to).

Pure COMPUTE (rule 7a): no I/O, no network. ``partition_index_preview`` is an
explainability aid, computed via a stable local hash — it is NOT guaranteed
to match Kafka's actual internal partitioner (default: murmur2) bit-for-bit;
the only guarantee this node makes is determinism and injectivity of the key
itself, which is what the mutual-exclusion invariant actually depends on.
"""

from __future__ import annotations

import hashlib

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ModelPartitionKeyRequest(BaseModel):
    """One (repo, branch) pair to derive a fleet-routing partition key for."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str = Field(
        ...,
        pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$",
        max_length=140,
        description="Repo slug (owner/name) — same pattern as "
        "ModelPushValidationRequest.repo.",
    )
    branch: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Bare branch name (no refs/heads/) — git ref grammar "
        "forbids ':' here, which is what makes the key join collision-safe.",
    )
    partition_count: int = Field(
        ...,
        gt=0,
        description="DECLARED target topology size (informational — this "
        "node performs no live Kafka topic mutation). Used only to compute "
        "the illustrative partition_index_preview.",
    )

    @model_validator(mode="after")
    def _branch_forbids_colon(self) -> ModelPartitionKeyRequest:
        if ":" in self.branch:
            raise ValueError(
                "branch must not contain ':' — this violates git's own ref "
                "grammar and would break the key join's injectivity "
                f"guarantee: {self.branch!r}"
            )
        return self


class ModelPartitionKeyResult(BaseModel):
    """Deterministic, injective routing key for one (repo, branch) pair."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str = Field(..., description="Echo of the request repo.")
    branch: str = Field(..., description="Echo of the request branch.")
    partition_key: str = Field(
        ...,
        description="f'{repo}:{branch}' — the value a Kafka producer should "
        "encode (utf-8) and pass as the message key so Kafka's own "
        "same-key-same-partition guarantee holds for this branch's stream.",
    )
    partition_index_preview: int = Field(
        ...,
        ge=0,
        description="Illustrative only: stable_hash(partition_key) %% "
        "partition_count. NOT guaranteed to match Kafka's actual "
        "partitioner (murmur2) — the real guarantee is on partition_key "
        "itself (deterministic + injective), not this preview index.",
    )
    partition_count: int = Field(
        ..., gt=0, description="Echo of the declared topology size."
    )


def derive_partition_key(repo: str, branch: str) -> str:
    """Pure key-derivation primitive — the single source of truth for the join.

    Kept as a standalone function (not only inlined in the handler) so a
    future EFFECT (the real Kafka producer wiring — not built this session)
    can import the identical derivation without re-deriving contract logic.
    """
    return f"{repo}:{branch}"


def stable_partition_index(partition_key: str, partition_count: int) -> int:
    """Deterministic, illustrative-only partition index preview.

    Uses blake2b (stdlib, no I/O) rather than Python's salted built-in
    ``hash()`` — ``hash()`` is randomized per-process (PYTHONHASHSEED) for
    strings, which would make this "deterministic" preview non-reproducible
    across processes/runs. This is NOT Kafka's murmur2 partitioner; see the
    model docstring.
    """
    digest = hashlib.blake2b(partition_key.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, byteorder="big") % partition_count


__all__ = [
    "ModelPartitionKeyRequest",
    "ModelPartitionKeyResult",
    "derive_partition_key",
    "stable_partition_index",
]
