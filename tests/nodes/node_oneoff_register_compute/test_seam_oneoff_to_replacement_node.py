# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The seam of the one-off register to its replacement node (OMN-19998): the register's node replacement ref names a node that exists among the nodes that do.

The seam loads the node's example overlay as the register policy and checks that every node
replacement ref names one of the node packages that exist. The failing control renames one ref
to a node that does not exist and reads the missing replacement node through
``HandlerOneOffRegister``, and the node list is the real list of the node packages.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_oneoff_register_compute.handlers.handler_oneoff_register import (
    HandlerOneOffRegister,
)
from omnimarket.nodes.node_oneoff_register_compute.models.model_oneoff_register import (
    EnumReplacementKind,
    ModelLivenessProcessRef,
    ModelOneOffRegisterPolicy,
    ModelOneOffRegisterRequest,
    ModelOneOffRegisterResult,
)
from omnimarket.nodes.node_oneoff_register_compute.runtime_oneoff_register import (
    list_node_names,
)

pytestmark = pytest.mark.unit

NODE_DIR = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_oneoff_register_compute"
)


def overlay_document() -> dict[str, object]:
    return yaml.safe_load(
        (NODE_DIR / "overlay.example.yaml").read_text(encoding="utf-8")
    )


def policy() -> ModelOneOffRegisterPolicy:
    return ModelOneOffRegisterPolicy.model_validate(overlay_document())


def read(
    the_policy: ModelOneOffRegisterPolicy, node_names: tuple[str, ...]
) -> ModelOneOffRegisterResult:
    liveness = tuple(
        ModelLivenessProcessRef(
            process_id=entry.liveness_process_id, host="example-host", state="active"
        )
        for entry in the_policy.entries
        if entry.liveness_process_id
    )
    return HandlerOneOffRegister().handle(
        ModelOneOffRegisterRequest(
            correlation_id="seam",
            policy=the_policy,
            liveness_processes=liveness,
            node_names=node_names,
        )
    )


def test_the_example_overlay_reads_as_the_register_policy() -> None:
    the_policy = policy()

    assert the_policy.scope_hosts == ("example-host",)
    assert the_policy.scope_states == ("active",)
    assert len(the_policy.entries) == 2
    assert len(the_policy.exclusions) == 1
    assert {
        entry.replacement_kind: entry.replacement_ref for entry in the_policy.entries
    } == {
        EnumReplacementKind.NODE: "node_start_path_census_compute",
        EnumReplacementKind.PLAN: "example-plan",
    }


def test_every_node_replacement_ref_names_a_node_that_exists() -> None:
    names = list_node_names()
    refs = [
        entry.replacement_ref
        for entry in policy().entries
        if entry.replacement_kind is EnumReplacementKind.NODE
    ]

    assert refs
    assert all(ref in names for ref in refs)


def test_the_node_list_is_the_node_packages_and_only_them() -> None:
    names = list_node_names()

    assert "node_oneoff_register_compute" in names
    assert names == tuple(sorted(names))
    assert all(name.startswith("node_") for name in names)


def test_a_renamed_replacement_node_fails_the_read() -> None:
    document = overlay_document()
    for row in document["entries"]:
        if row["replacement_kind"] == "node":
            row["replacement_ref"] = "node_example_renamed"
    renamed = ModelOneOffRegisterPolicy.model_validate(document)

    broken = read(renamed, list_node_names())
    assert broken.passed is False
    assert (
        "replacement_node_missing:node_example_renamed" in broken.readings[0].findings
    )

    clean = read(policy(), list_node_names())
    assert clean.passed is True
