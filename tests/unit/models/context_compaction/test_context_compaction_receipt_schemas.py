# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The six compaction receipt events and their topics (OMN-21112, TA.10).

Each receipt is one concept on its own ``onex.evt.omnimarket`` topic, carries
``session_id`` (the partition key) and names its contracted identity field in
``IDENTITY_FIELD``. No receipt holds a free-text field: every string the schema
can hold is an id, a hash, an enum member or a name, each fenced by an enum or
a pattern.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import yaml
from pydantic import BaseModel, ValidationError

from omnimarket.events import topics
from omnimarket.models.context_compaction import (
    COMPACTION_RECEIPT_MODELS,
    ModelContextCompactionFallback,
    ModelContextCompactionInstalled,
    ModelContextCompactionInstallMismatch,
    ModelContextCompactionInstallUncertain,
    ModelContextCompactionPrepared,
)

_SHA = "a" * 64
_ORCHESTRATOR_CONTRACT = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_context_compaction_orchestrator/contract.yaml"
)
_RECEIPT_TOPICS = (
    topics.CONTEXT_COMPACTION_PREPARED_TOPIC_V1,
    topics.CONTEXT_COMPACTION_ACTIVATED_TOPIC_V1,
    topics.CONTEXT_COMPACTION_FALLBACK_TOPIC_V1,
    topics.CONTEXT_COMPACTION_INSTALLED_TOPIC_V1,
    topics.CONTEXT_COMPACTION_INSTALL_MISMATCH_TOPIC_V1,
    topics.CONTEXT_COMPACTION_INSTALL_UNCERTAIN_TOPIC_V1,
)
_IDENTITIES = {
    "ModelContextCompactionPrepared": "candidate_id",
    "ModelContextCompactionActivated": "activation_id",
    "ModelContextCompactionFallback": "invocation_id",
    "ModelContextCompactionInstalled": "activation_id",
    "ModelContextCompactionInstallMismatch": "activation_id",
    "ModelContextCompactionInstallUncertain": "activation_id",
}


def test_six_receipts_each_on_a_topic_of_their_own() -> None:
    assert len(COMPACTION_RECEIPT_MODELS) == 6
    assert len(set(_RECEIPT_TOPICS)) == 6
    for topic in _RECEIPT_TOPICS:
        assert topic.startswith("onex.evt.omnimarket.context-compaction-")
    for topic in (
        topics.CONTEXT_COMPACTION_REQUESTED_TOPIC_V1,
        topics.CONTEXT_COMPACTION_PACKAGE_TOPIC_V1,
    ):
        assert topic.startswith("onex.cmd.omniintelligence.context-compaction-")


def test_topic_constants_agree_with_the_orchestrator_contract() -> None:
    contract = yaml.safe_load(_ORCHESTRATOR_CONTRACT.read_text(encoding="utf-8"))
    declared = {
        *contract["event_bus"]["subscribe_topics"],
        *contract["event_bus"]["publish_topics"],
    }
    assert declared == {
        topics.CONTEXT_COMPACTION_REQUESTED_TOPIC_V1,
        topics.CONTEXT_COMPACTION_PACKAGE_TOPIC_V1,
        topics.CONTEXT_COMPACTION_PREPARED_TOPIC_V1,
        topics.CONTEXT_COMPACTION_ACTIVATED_TOPIC_V1,
        topics.CONTEXT_COMPACTION_FALLBACK_TOPIC_V1,
    }


@pytest.mark.parametrize("model", COMPACTION_RECEIPT_MODELS, ids=lambda m: m.__name__)
def test_each_receipt_names_its_identity_and_partition_key(
    model: type[BaseModel],
) -> None:
    identity = vars(model)["IDENTITY_FIELD"]
    assert identity == _IDENTITIES[model.__name__]
    assert model.model_fields[identity].is_required()
    assert model.model_fields[identity].annotation is UUID
    assert model.model_fields["session_id"].is_required()


@pytest.mark.parametrize("model", COMPACTION_RECEIPT_MODELS, ids=lambda m: m.__name__)
def test_receipts_are_frozen_and_refuse_unknown_fields(model: type[BaseModel]) -> None:
    assert model.model_config.get("extra") == "forbid"
    assert model.model_config.get("frozen") is True


def _free_text_paths(schema: dict[str, Any]) -> list[str]:
    """Paths of string leaves bounded by neither an enum, a pattern, a format nor a const."""
    defs: dict[str, Any] = schema.get("$defs", {})
    found: list[str] = []

    def walk(node: Any, path: str, seen: frozenset[str]) -> None:
        if not isinstance(node, dict):
            return
        ref = node.get("$ref")
        if isinstance(ref, str):
            name = ref.rsplit("/", 1)[-1]
            if name not in seen:
                walk(defs[name], path, seen | {name})
            return
        if node.get("type") == "string":
            if not ({"format", "enum", "pattern", "const"} & node.keys()):
                found.append(path)
            return
        for key in ("anyOf", "oneOf", "allOf", "prefixItems"):
            for index, child in enumerate(node.get(key, [])):
                walk(child, f"{path}[{key}{index}]", seen)
        if "items" in node:
            walk(node["items"], f"{path}[]", seen)
        for name, child in node.get("properties", {}).items():
            walk(child, f"{path}.{name}", seen)

    walk(schema, "", frozenset())
    return found


@pytest.mark.parametrize("model", COMPACTION_RECEIPT_MODELS, ids=lambda m: m.__name__)
def test_receipt_schema_holds_no_free_text_field(model: type[BaseModel]) -> None:
    assert _free_text_paths(model.model_json_schema()) == []


def test_free_text_walk_positive_control() -> None:
    class _Leaky(BaseModel):
        session_id: str
        note: str | None = None

    assert _free_text_paths(_Leaky.model_json_schema()) == [
        ".session_id",
        ".note[anyOf0]",
    ]


def test_receipts_refuse_free_text_in_a_bounded_field() -> None:
    with pytest.raises(ValidationError):
        ModelContextCompactionPrepared(
            candidate_id=uuid4(),
            session_id="s-1",
            input_messages_hash=_SHA,
            assembly_hash="the package summarised the session",
            version_vector_hash=_SHA,
            profile_name="compaction_orchestrator",
            profile_content_hash=_SHA,
        )
    with pytest.raises(ValidationError):
        ModelContextCompactionFallback.model_validate(
            {
                "invocation_id": uuid4(),
                "session_id": "s-1",
                "failure_class": "the barrier timed out",
            }
        )
    with pytest.raises(ValidationError):
        ModelContextCompactionInstalled(
            activation_id=uuid4(),
            session_id="the user asked for a refactor",
            boundary_uuid="b-1",
            observed_hash=_SHA,
        )


def test_not_installed_is_an_uncertain_reason_not_a_topic() -> None:
    receipt = ModelContextCompactionInstallUncertain.model_validate(
        {"activation_id": uuid4(), "session_id": "s-1", "reason": "native_compaction"}
    )
    assert receipt.boundary_uuid is None
    assert receipt.model_dump(mode="json")["reason"] == "native_compaction"
    with pytest.raises(ValidationError):
        ModelContextCompactionInstallUncertain.model_validate(
            {"activation_id": uuid4(), "session_id": "s-1", "reason": "not_installed"}
        )


def test_installation_receipts_round_trip_through_json() -> None:
    activation = uuid4()
    mismatch = ModelContextCompactionInstallMismatch(
        activation_id=activation,
        session_id="s-1",
        boundary_uuid="b-1",
        installed_messages_hash=_SHA,
        observed_hash="b" * 64,
    )
    assert (
        ModelContextCompactionInstallMismatch.model_validate_json(
            mismatch.model_dump_json()
        )
        == mismatch
    )
