# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Model serving and lane placement are independent host-table facts."""

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.handlers.overlay_keys import resolve_lab_hosts_table
from omnimarket.models.overlay_keys import ModelLabHostRow
from omnimarket.nodes.node_lab_placement_compute.handlers.handler_lab_placement import (
    HandlerLabPlacement,
)

pytestmark = pytest.mark.unit

# The reproducer's two model hosts, with neutral destinations and reasons.
RECORDED_HOSTS = [
    {
        "name": "h202",
        "target": "192.0.2.202",
        "available": False,
        "unavailable_reason": "host down for repair",
        "model_port": 8000,
        "model_serving": False,
    },
    {
        "name": "h201",
        "target": "192.0.2.201",
        "available": False,
        "unavailable_reason": "closed to lane placement",
        "model_port": 8000,
        "model_serving": True,
    },
]


@pytest.mark.parametrize("source", ["overlay", "file"])
def test_recorded_serving_hosts_resolve_without_reopening_placement(
    tmp_path: Path, source: str
) -> None:
    document: dict[str, object] = {"hosts": RECORDED_HOSTS}
    if source == "overlay":
        path = tmp_path / "lab_hosts" / "overlay.yaml"
        document["registry_root_candidates"] = ["/srv/example/registry"]
        environ = {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)}
    else:
        path = tmp_path / "hosts.yaml"
        environ = {"ONEX_LAB_RUN_HOSTS": str(path)}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document), encoding="utf-8")

    table = resolve_lab_hosts_table(environ)
    assert [row.model_serving for row in table.hosts] == [False, True]
    assert [row.available for row in table.hosts] == [False, False]
    assert [row.model_dump()["model_serving"] for row in table.hosts] == [False, True]

    placement = HandlerLabPlacement()
    unit = placement.unit_profile(repo="omnimarket", kind="test", weight=None)
    for row in table.hosts:
        verdict = placement.judge_host(row.name, row, unit)
        assert not verdict.eligible
        assert row.unavailable_reason in verdict.reason


@pytest.mark.parametrize("available", [True, False])
def test_omitted_serving_inherits_existing_availability(available: bool) -> None:
    row = ModelLabHostRow(name="host-a", target="192.0.2.10", available=available)
    assert row.model_serving is available
    assert row.available is available


def test_omitted_availability_and_serving_keep_the_available_default() -> None:
    row = ModelLabHostRow(name="host-a", target="192.0.2.10")
    assert row.available is True
    assert row.model_serving is True


@pytest.mark.parametrize("available", [True, False])
@pytest.mark.parametrize("model_serving", [True, False])
def test_explicit_serving_is_independent_of_placement(
    available: bool, model_serving: bool
) -> None:
    row = ModelLabHostRow.model_validate(
        {
            "name": "host-a",
            "target": "192.0.2.10",
            "available": available,
            "model_serving": model_serving,
        }
    )
    assert row.available is available
    assert row.model_serving is model_serving
    placement = HandlerLabPlacement()
    unit = placement.unit_profile(repo="omnimarket", kind="test", weight=None)
    assert placement.judge_host(row.name, row, unit).eligible is available
    with pytest.raises(ValidationError, match="frozen_instance"):
        row.model_serving = not model_serving


@pytest.mark.parametrize("value", [None, "not-a-boolean", [], {}])
def test_malformed_serving_is_refused_by_the_resolver(
    tmp_path: Path, value: object
) -> None:
    path = tmp_path / "hosts.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "hosts": [
                    {"name": "host-a", "target": "192.0.2.10", "model_serving": value}
                ]
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(NodeOverlayError, match=r"hosts\.0\.model_serving"):
        resolve_lab_hosts_table({"ONEX_LAB_RUN_HOSTS": str(path)})


def test_unknown_host_field_is_still_refused(tmp_path: Path) -> None:
    path = tmp_path / "hosts.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "hosts": [
                    {"name": "host-a", "target": "192.0.2.10", "serving_typo": True}
                ]
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(NodeOverlayError, match=r"hosts\.0\.serving_typo"):
        resolve_lab_hosts_table({"ONEX_LAB_RUN_HOSTS": str(path)})
