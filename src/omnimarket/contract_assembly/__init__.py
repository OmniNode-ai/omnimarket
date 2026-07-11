# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared model->contract serialization library.

Single definition home for the serialization pipeline's typed field boundaries
and pure functions. The five serialization compute nodes each wrap one function
here, so no node imports another node's models -- the assembler consumes rendered
subcontracts by type through the shared :mod:`omnimarket.contract_assembly.models`
package.
"""

from __future__ import annotations

from omnimarket.contract_assembly.advanced_features import (
    archetype_defaults,
    resolve_advanced_features,
)
from omnimarket.contract_assembly.assemble import assemble_contract
from omnimarket.contract_assembly.digest import digest_contract
from omnimarket.contract_assembly.lint import lint_contract
from omnimarket.contract_assembly.render import canonical_operations, render_subcontract
from omnimarket.contract_assembly.serialize import serialize_contract

__all__ = [
    "archetype_defaults",
    "assemble_contract",
    "canonical_operations",
    "digest_contract",
    "lint_contract",
    "render_subcontract",
    "resolve_advanced_features",
    "serialize_contract",
]
