# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The edge overlay file: the example loads whole, and one mutation refuses it.

Golden: ``overlay.example.yaml`` loads through ``ModelEdgeOverlayFile`` and
through ``load_overlay`` with ``ONEX_SKILL_OVERLAY_ROOTS`` pointing at a root
holding ``node_federation_edge_decision_compute/overlay.yaml`` copied from the
example, and both give the same overlay. Error chain: every required key gone,
one case per key at every level; an unknown key wherever it is planted, a
``reply`` key and a second ``directions`` list in the file; a wrong literal
(``name``, ``delivery_mode``, ``to.type``, ``classes``, the receipt's pinned
``effect_class`` and ``item_kind``, ``producer_acks``, ``idempotent_producer``);
an unbounded or unanchored ``key_pattern``; an empty header allow-list; a zero
``acl_probe_max_age_seconds`` and ``max_headers``; and every cross-field
disagreement -- the source runtime, the auth and ACL principals, the
destination and origin lanes, the message-id header outside the allow-list, a
stamp header name inside it, and a pinned physical topic that is not the one
resolver's answer (one character changed, or another tenant's). Failing
control: the unmutated document loads, so each refusal is the mutation's, not
the example's.
"""

from __future__ import annotations

import copy
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_federation_edge_decision_compute.models.model_edge_overlay import (
    ModelEdgeOverlayFile,
)
from omnimarket.nodes.node_federation_edge_decision_compute.overlay import (
    NODE_NAME,
    load_overlay,
)

pytestmark = pytest.mark.unit

OVERLAY_EXAMPLE = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_federation_edge_decision_compute/overlay.example.yaml"
)

ALLOWED_TOPIC = "onex.cmd.omnimarket.lifecycle-start-requested.v1"
PINNED_PHYSICAL_TOPIC = (
    "tenant-example-tenant-a.onex.cmd.omnimarket.lifecycle-start-requested.v1"
)
# The pinned topic with exactly one character changed (the trailing 1 of .v1).
PINNED_ONE_CHARACTER_CHANGED = (
    "tenant-example-tenant-a.onex.cmd.omnimarket.lifecycle-start-requested.v2"
)
# The pinned topic the resolver answers for a different tenant's slug.
PINNED_OTHER_TENANT = (
    "tenant-example-tenant-b.onex.cmd.omnimarket.lifecycle-start-requested.v1"
)
UUID_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"


def _document() -> dict[str, Any]:
    """A deep copy of the example's document, for exactly one mutation each."""
    return copy.deepcopy(yaml.safe_load(OVERLAY_EXAMPLE.read_text(encoding="utf-8")))


def _node(document: dict[str, Any], path: tuple[Any, ...]) -> Any:
    """The document reached by walking ``path`` (dict keys and list indices)."""
    node: Any = document
    for step in path:
        node = node[step]
    return node


def _set(document: dict[str, Any], path: tuple[Any, ...], value: Any) -> None:
    _node(document, path[:-1])[path[-1]] = value


def _delete(document: dict[str, Any], path: tuple[Any, ...]) -> None:
    del _node(document, path[:-1])[path[-1]]


class TestGoldenChain:
    def test_the_example_overlay_loads_through_the_model(self) -> None:
        overlay = ModelEdgeOverlayFile.model_validate(_document())
        assert overlay.edge_id == "example-inmemory-to-lane"
        assert overlay.binding.runtime_ref == "example-runtime-a"
        assert overlay.binding.principal_ref == "example-edge-writer"
        assert overlay.binding.permitted_lanes == ("example-lane",)
        assert overlay.direction.name == "up"
        assert overlay.direction.allowed_topic == ALLOWED_TOPIC
        assert overlay.direction.pinned_physical_topic == PINNED_PHYSICAL_TOPIC
        assert overlay.direction.key_binding == "command_id"
        assert overlay.direction.key_pattern == UUID_PATTERN
        assert overlay.direction.to.lane == "example-lane"
        assert overlay.direction.header_allow_list[0].name == "message-id"

    def test_the_example_overlay_loads_through_the_overlay_reader(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        folder = tmp_path / NODE_NAME
        folder.mkdir(parents=True)
        shutil.copyfile(OVERLAY_EXAMPLE, folder / "overlay.yaml")
        monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
        overlay = load_overlay()
        assert overlay == ModelEdgeOverlayFile.model_validate(_document())
        assert overlay.direction.pinned_physical_topic == PINNED_PHYSICAL_TOPIC


class TestFailingControl:
    def test_the_unmutated_example_document_loads(self) -> None:
        overlay = ModelEdgeOverlayFile.model_validate(_document())
        assert overlay.binding.origin_lane == "example-lane"
        assert overlay.direction.message_id_header == "message-id"


class TestMissingKey:
    """Every required key gone, one case per key at every level of the file."""

    CASES = (
        pytest.param((), "binding", id="top-binding"),
        pytest.param((), "edge_id", id="top-edge_id"),
        pytest.param((), "direction", id="top-direction"),
        pytest.param(("binding",), "runtime_ref", id="binding-runtime_ref"),
        pytest.param(("binding",), "principal_ref", id="binding-principal_ref"),
        pytest.param(("binding",), "tenant_slug_ref", id="binding-tenant_slug_ref"),
        pytest.param(("binding",), "origin_lane", id="binding-origin_lane"),
        pytest.param(("binding",), "permitted_lanes", id="binding-permitted_lanes"),
        pytest.param(("direction",), "name", id="direction-name"),
        pytest.param(("direction",), "from", id="direction-from"),
        pytest.param(("direction",), "to", id="direction-to"),
        pytest.param(("direction",), "allowed_topic", id="direction-allowed_topic"),
        pytest.param(
            ("direction",),
            "pinned_physical_topic",
            id="direction-pinned_physical_topic",
        ),
        pytest.param(("direction",), "key_pattern", id="direction-key_pattern"),
        pytest.param(("direction",), "key_binding", id="direction-key_binding"),
        pytest.param(
            ("direction",), "header_allow_list", id="direction-header_allow_list"
        ),
        pytest.param(("direction",), "max_headers", id="direction-max_headers"),
        pytest.param(("direction",), "max_value_bytes", id="direction-max_value_bytes"),
        pytest.param(("direction",), "max_value_depth", id="direction-max_value_depth"),
        pytest.param(("direction",), "stamp_headers", id="direction-stamp_headers"),
        pytest.param(
            ("direction",), "message_id_header", id="direction-message_id_header"
        ),
        pytest.param(("direction",), "classes", id="direction-classes"),
        pytest.param(("direction",), "delivery_mode", id="direction-delivery_mode"),
        pytest.param(
            ("direction",), "wire_topic_mapping", id="direction-wire_topic_mapping"
        ),
        pytest.param(("direction",), "receipt", id="direction-receipt"),
        pytest.param(("direction",), "acl", id="direction-acl"),
        pytest.param(("direction", "from"), "type", id="from-type"),
        pytest.param(("direction", "from"), "runtime_ref", id="from-runtime_ref"),
        pytest.param(("direction", "from"), "max_events", id="from-max_events"),
        pytest.param(
            ("direction", "from"),
            "flush_timeout_seconds",
            id="from-flush_timeout_seconds",
        ),
        pytest.param(("direction", "to"), "type", id="to-type"),
        pytest.param(("direction", "to"), "lane", id="to-lane"),
        pytest.param(("direction", "to"), "auth_ref", id="to-auth_ref"),
        pytest.param(("direction", "to"), "producer_acks", id="to-producer_acks"),
        pytest.param(
            ("direction", "to"), "idempotent_producer", id="to-idempotent_producer"
        ),
        pytest.param(("direction", "to"), "acl_probe_ref", id="to-acl_probe_ref"),
        pytest.param(
            ("direction", "to"),
            "acl_probe_max_age_seconds",
            id="to-acl_probe_max_age_seconds",
        ),
        pytest.param(
            ("direction", "receipt"), "request_topic", id="receipt-request_topic"
        ),
        pytest.param(
            ("direction", "receipt"), "record_topic", id="receipt-record_topic"
        ),
        pytest.param(
            ("direction", "receipt"), "refusal_topic", id="receipt-refusal_topic"
        ),
        pytest.param(
            ("direction", "receipt"), "wait_seconds", id="receipt-wait_seconds"
        ),
        pytest.param(
            ("direction", "receipt"),
            "in_doubt_scan_max_records",
            id="receipt-in_doubt_scan_max_records",
        ),
        pytest.param(
            ("direction", "receipt"),
            "in_doubt_max_age_seconds",
            id="receipt-in_doubt_max_age_seconds",
        ),
        pytest.param(
            ("direction", "receipt"), "effect_class", id="receipt-effect_class"
        ),
        pytest.param(("direction", "receipt"), "item_kind", id="receipt-item_kind"),
        pytest.param(
            ("direction", "acl"), "write_principal_ref", id="acl-write_principal_ref"
        ),
        pytest.param(
            ("direction", "acl"), "read_principal_ref", id="acl-read_principal_ref"
        ),
        pytest.param(
            ("direction", "acl"), "read_group_prefix", id="acl-read_group_prefix"
        ),
        pytest.param(
            ("direction", "stamp_headers"),
            "origin_lane",
            id="stamp_headers-origin_lane",
        ),
        pytest.param(
            ("direction", "stamp_headers"),
            "source_lane",
            id="stamp_headers-source_lane",
        ),
        pytest.param(("direction", "stamp_headers"), "hops", id="stamp_headers-hops"),
        pytest.param(
            ("direction", "header_allow_list", 0), "name", id="header-rule-name"
        ),
        pytest.param(
            ("direction", "header_allow_list", 0), "pattern", id="header-rule-pattern"
        ),
    )

    @pytest.mark.parametrize(("parent", "key"), CASES)
    def test_the_key_gone_refuses_the_load(
        self, parent: tuple[Any, ...], key: Any
    ) -> None:
        document = _document()
        _delete(document, (*parent, key))
        with pytest.raises(ValidationError):
            ModelEdgeOverlayFile.model_validate(document)


class TestUnknownKey:
    CASES = (
        pytest.param((), "reply_to", "example-lane", id="top"),
        pytest.param(("direction",), "unheard_of", "example", id="direction"),
        pytest.param(
            ("direction",),
            "reply",
            {"lane": "example-lane", "type": "kafka"},
            id="direction-reply",
        ),
        pytest.param(("direction", "from"), "unheard_of", "example", id="from"),
        pytest.param(
            ("direction", "to"),
            "signing_key_ref",
            "example-signing-key",
            id="to-signing_key_ref",
        ),
    )

    @pytest.mark.parametrize(("parent", "key", "value"), CASES)
    def test_the_key_planted_refuses_the_load(
        self, parent: tuple[Any, ...], key: Any, value: Any
    ) -> None:
        document = _document()
        _set(document, (*parent, key), value)
        with pytest.raises(ValidationError):
            ModelEdgeOverlayFile.model_validate(document)

    def test_a_second_direction_as_a_directions_list_refuses_the_load(self) -> None:
        document = _document()
        document["directions"] = [copy.deepcopy(document["direction"])]
        with pytest.raises(ValidationError):
            ModelEdgeOverlayFile.model_validate(document)


class TestWrongValue:
    """A value the file's own pinned literals and bounds refuse."""

    CASES = (
        pytest.param(("direction", "name"), "down", id="name-down"),
        pytest.param(
            ("direction", "delivery_mode"), "continuous", id="delivery_mode-continuous"
        ),
        pytest.param(("direction", "to", "type"), "https", id="to-type-https"),
        pytest.param(("direction", "classes"), ["tenant"], id="classes-tenant"),
        pytest.param(
            ("direction", "classes"), ["tenant_metadata"], id="classes-tenant_metadata"
        ),
        pytest.param(
            ("direction", "classes"),
            ["internal", "tenant"],
            id="classes-internal-tenant",
        ),
        pytest.param(("direction", "to", "producer_acks"), 1, id="producer_acks-1"),
        pytest.param(
            ("direction", "to", "idempotent_producer"), False, id="idempotent_false"
        ),
        pytest.param(
            ("direction", "receipt", "effect_class"),
            "act_then_read",
            id="receipt-effect_class",
        ),
        pytest.param(
            ("direction", "receipt", "item_kind"), "batch", id="receipt-item_kind"
        ),
        pytest.param(("direction", "key_pattern"), "^[a-z]*$", id="key_pattern-star"),
        pytest.param(("direction", "key_pattern"), "^[a-z]+$", id="key_pattern-plus"),
        pytest.param(
            ("direction", "key_pattern"),
            "^[a-z]{2,}$",
            id="key_pattern-unbounded-braces",
        ),
        pytest.param(
            ("direction", "key_pattern"),
            "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            id="key_pattern-no-trailing-dollar",
        ),
        pytest.param(
            ("direction", "key_pattern"),
            "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
            id="key_pattern-no-leading-caret",
        ),
        pytest.param(
            ("direction", "key_pattern"),
            "[0-9a-f]{8}-[0-9a-f]{4}",
            id="key_pattern-unanchored",
        ),
        pytest.param(
            ("direction", "header_allow_list"), [], id="header_allow_list-empty"
        ),
        pytest.param(
            ("direction", "to", "acl_probe_max_age_seconds"),
            0,
            id="acl_probe_max_age_seconds-zero",
        ),
        pytest.param(("direction", "max_headers"), 0, id="max_headers-zero"),
    )

    @pytest.mark.parametrize(("path", "value"), CASES)
    def test_the_value_refuses_the_load(
        self, path: tuple[Any, ...], value: Any
    ) -> None:
        document = _document()
        _set(document, path, value)
        with pytest.raises(ValidationError):
            ModelEdgeOverlayFile.model_validate(document)


class TestCrossFieldDisagreement:
    """A field that disagrees with another field the file's own validator reads."""

    CASES = (
        pytest.param(
            ("direction", "pinned_physical_topic"),
            PINNED_ONE_CHARACTER_CHANGED,
            id="pinned_one_character_changed",
        ),
        pytest.param(
            ("direction", "pinned_physical_topic"),
            PINNED_OTHER_TENANT,
            id="pinned_another_tenants_topic",
        ),
        pytest.param(
            ("direction", "from", "runtime_ref"),
            "example-runtime-z",
            id="from_runtime_ref",
        ),
        pytest.param(
            ("direction", "to", "auth_ref"), "example-edge-writer-z", id="to_auth_ref"
        ),
        pytest.param(
            ("direction", "acl", "write_principal_ref"),
            "example-edge-writer-z",
            id="acl_write_principal_ref",
        ),
        pytest.param(
            ("direction", "to", "lane"), "example-lane-z", id="to_lane_not_permitted"
        ),
        pytest.param(
            ("binding", "origin_lane"),
            "example-lane-z",
            id="origin_lane_not_permitted",
        ),
        pytest.param(
            ("direction", "message_id_header"),
            "correlation-id",
            id="message_id_header_not_allowed",
        ),
    )

    @pytest.mark.parametrize(("path", "value"), CASES)
    def test_the_disagreement_refuses_the_load(
        self, path: tuple[Any, ...], value: Any
    ) -> None:
        document = _document()
        _set(document, path, value)
        with pytest.raises(ValidationError):
            ModelEdgeOverlayFile.model_validate(document)

    def test_a_stamp_header_name_inside_the_allow_list_refuses_the_load(self) -> None:
        document = _document()
        document["direction"]["header_allow_list"].append(
            {"name": "hops", "pattern": "^[0-9]{1,2}$"}
        )
        with pytest.raises(ValidationError):
            ModelEdgeOverlayFile.model_validate(document)
