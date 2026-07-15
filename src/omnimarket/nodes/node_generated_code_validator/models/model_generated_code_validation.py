"""Result model: typed diagnostics for a generated code artifact.

Canonical definition lives in ``omnimarket.codegen.models`` (a shared,
cross-node package) so ``node_codegen_outcome_reducer`` can consume this
node's verdict without reaching into its private ``models`` package
(OMN-9263 doctrine / OMN-14608). This module re-exports the same class —
identity is preserved, so this node's own handler and contract keep working
unchanged.
"""

from __future__ import annotations

from omnimarket.codegen.models import ModelGeneratedCodeValidation

__all__ = ["ModelGeneratedCodeValidation"]
