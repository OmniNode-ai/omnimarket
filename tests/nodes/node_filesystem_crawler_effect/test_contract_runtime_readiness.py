# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Runtime-readiness checks for node_filesystem_crawler_effect."""

from __future__ import annotations

import importlib
import inspect
from pathlib import Path
from typing import Any

import yaml

CONTRACT_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_filesystem_crawler_effect"
    / "contract.yaml"
)


def _load_contract() -> dict[str, Any]:
    with CONTRACT_PATH.open() as handle:
        contract = yaml.safe_load(handle)
    assert isinstance(contract, dict)
    return contract


def _load_handler(handler_ref: dict[str, str]) -> type:
    module = importlib.import_module(handler_ref["module"])
    handler = getattr(module, handler_ref["name"])
    assert isinstance(handler, type)
    return handler


def test_runtime_contract_uses_market_filesystem_crawl_handler() -> None:
    contract = _load_contract()
    handler = contract["handler"]
    routing_handlers = contract["handler_routing"]["handlers"]

    assert handler["class"] == "HandlerCrawlFilesystem"
    assert handler["module"].endswith("handler_crawl_filesystem")
    assert contract["input_model"] == "ModelCrawlFilesystemRequest"
    assert contract["output_model"] == "ModelCrawlFilesystemResult"

    assert len(routing_handlers) == 1
    entry = routing_handlers[0]
    assert entry["operation"] == "crawl_filesystem"
    assert entry["handler"] == {
        "name": "HandlerCrawlFilesystem",
        "module": (
            "omnimarket.nodes.node_filesystem_crawler_effect.handlers."
            "handler_crawl_filesystem"
        ),
    }


def test_runtime_contract_does_not_autowire_crawl_state_handler() -> None:
    contract = _load_contract()
    routed_handler_names = {
        entry["handler"]["name"] for entry in contract["handler_routing"]["handlers"]
    }

    assert "HandlerFilesystemCrawler" not in routed_handler_names
    assert contract["handler"]["class"] != "HandlerFilesystemCrawler"
    assert "dependencies" not in contract


def test_runtime_wired_handler_is_zero_arg_constructible() -> None:
    contract = _load_contract()
    handler_type = _load_handler(contract["handler_routing"]["handlers"][0]["handler"])
    signature = inspect.signature(handler_type)
    required_parameters = [
        parameter
        for parameter in signature.parameters.values()
        if parameter.default is inspect.Parameter.empty
        and parameter.kind
        in {
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        }
    ]

    assert required_parameters == []
    assert handler_type().__class__.__name__ == "HandlerCrawlFilesystem"


def test_runtime_contract_uses_market_command_topic() -> None:
    contract = _load_contract()
    event_bus = contract["event_bus"]

    assert event_bus["subscribe_topics"] == [
        "onex.cmd.omnimarket.filesystem-crawl-start.v1"
    ]
    # Primary market topic is declared; omnimemory lifecycle topics are also
    # declared as configurable publish topics (OMN-11061: wire undeclared literals).
    assert "onex.evt.omnimarket.content-discovered.v1" in event_bus["publish_topics"]
    assert "onex.cmd.omnimemory.crawl-tick.v1" not in event_bus["subscribe_topics"]
