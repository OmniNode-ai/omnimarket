# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Resolve and execute the packaged disk hygiene contract with fake effects."""

from __future__ import annotations

import importlib
from importlib.resources import files
from pathlib import Path

import yaml

from omnimarket.nodes.node_lab_disk_hygiene_effect.models import (
    ModelLabDiskHygieneDeployment,
)

from .test_lab_disk_hygiene_effect import CONTAINERS, FakeDocker, _census


def test_golden_chain_lab_disk_hygiene_effect(tmp_path: Path) -> None:
    name = "node_lab_disk_hygiene_effect"
    contract = yaml.safe_load(
        files(f"omnimarket.nodes.{name}").joinpath("contract.yaml").read_text()
    )
    assert contract["name"] == name
    assert contract["node_type"] == "effect"
    assert contract["lifecycle"] == "experimental"
    assert "event_bus" not in contract
    assert "terminal_event" not in contract
    routing = contract["handler_routing"]
    assert routing["routing_strategy"] == "operation_match"
    assert {entry["operation"] for entry in routing["handlers"]} == {
        "run_lab_disk_hygiene"
    }
    deployment = ModelLabDiskHygieneDeployment(_census(tmp_path).name)
    for entry in routing["handlers"]:
        handler_type = getattr(
            importlib.import_module(entry["handler"]["module"]),
            entry["handler"]["name"],
        )
        input_module, _, input_name = entry["input_model"].rpartition(".")
        output_module, _, output_name = entry["output_model"].rpartition(".")
        request_type = getattr(importlib.import_module(input_module), input_name)
        result_type = getattr(importlib.import_module(output_module), output_name)
        docker = FakeDocker(CONTAINERS)
        handler = handler_type(
            overlay=deployment,
            run=docker.run,
            disk_free=docker.disk_free,
            which=lambda _: "docker",
            held_paths=set,
        )
        request = request_type(omni_home=tmp_path)
        result = handler.handle(request)
        assert isinstance(request, request_type)
        assert isinstance(result, result_type)
        assert result.docker == "ran"
        assert set(result.removed_containers) == {"landing-l9-pg", "rlane-x-pg"}
        assert not result.errors
        assert {step.step for step in result.steps} == {
            "containers",
            "images",
            "build_cache",
            "volumes",
        }
