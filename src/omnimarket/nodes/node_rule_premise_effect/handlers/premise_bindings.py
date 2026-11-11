# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Overlay factory bridge. Part A storage and core validation remain with their owners."""

from collections.abc import Mapping
from functools import cache
from importlib import import_module
from typing import cast

from omnimarket.protocols.protocol_rule_premise import (
    ProtocolBatchClaimStore,
    ProtocolPremiseReader,
    ProtocolPremiseValidator,
    ProtocolRulePremiseGate,
)

from .batch_claim_store import MemoryBatchClaimStore


def memory_batch_claim_store(config: Mapping[str, str]) -> object:
    return _memory_claims(config["store_id"])


@cache
def _memory_claims(_store_id: str) -> MemoryBatchClaimStore:
    return MemoryBatchClaimStore()


def part_a_batch_claim_store(config: Mapping[str, str]) -> object:
    module = import_module("omnibase_infra.runtime.premise_admission")
    store = cast(ProtocolBatchClaimStore, module.create_batch_claim_store(config))
    if not isinstance(store, ProtocolBatchClaimStore):
        raise TypeError("Part A batch claim factory does not satisfy its protocol")
    return store


def part_a_premise_reader(config: Mapping[str, str]) -> object:
    module = import_module("omnibase_infra.runtime.premise_admission")
    reader = cast(ProtocolPremiseReader, module.create_premise_reader(config))
    if not isinstance(reader, ProtocolPremiseReader):
        raise TypeError("Part A premise reader factory does not satisfy its protocol")
    return reader


def core_premise_validator(config: Mapping[str, str]) -> object:
    module = import_module("omnibase_infra.runtime.premise_admission")
    validator = cast(ProtocolPremiseValidator, module.create_premise_validator(config))
    if not isinstance(validator, ProtocolPremiseValidator):
        raise TypeError("core premise validator factory does not satisfy its protocol")
    return validator


def rule_premise_bus_gate(config: Mapping[str, str]) -> object:
    module = import_module("omnibase_infra.runtime.premise_admission")
    gate = cast(ProtocolRulePremiseGate, module.create_gate(config))
    if not isinstance(gate, ProtocolRulePremiseGate):
        raise TypeError("R-B bus gate factory does not satisfy its protocol")
    return gate
