# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read continuation configuration from the node contract."""

import importlib
from pathlib import Path

import yaml

from ..models.model_policy import ModelContinuationPolicy, ModelHandlerComponent

NODE_ROOT = Path(__file__).resolve().parent.parent


def load_continuation_policy() -> ModelContinuationPolicy:
    return ModelContinuationPolicy.model_validate(
        yaml.safe_load((NODE_ROOT / "contract.yaml").read_text())["continuation"]
    )


def default_stability_path() -> Path:
    return NODE_ROOT / load_continuation_policy().stability_data_file


def load_component_handler[T](name: str, expected: type[T]) -> type[T]:
    contract = yaml.safe_load((NODE_ROOT / "contract.yaml").read_text())
    binding = ModelHandlerComponent.model_validate(contract["components"][name]).handler
    handler = getattr(importlib.import_module(binding.module), binding.name)
    if not isinstance(handler, type) or not issubclass(handler, expected):
        raise TypeError(f"component {name} must implement {expected.__name__}")
    return handler
