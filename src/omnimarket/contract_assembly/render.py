# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""L1: render a single subcontract YAML fragment for a given type.

One discriminated-union function over :class:`EnumSubcontractType` replaces the
per-type generator classes of the dormant serializer. Each type has a canonical
default operation list (data, not code); callers may override the operations or
add type-specific scalar fields. The rendered fragment is deterministic YAML text
plus its content digest.
"""

from __future__ import annotations

import hashlib
from typing import Any

import yaml

from omnimarket.contract_assembly.models import (
    EnumSubcontractType,
    ModelSubcontractFragment,
    ModelSubcontractRenderRequest,
)

# Canonical operation lists per subcontract type. This is contract DATA: the
# discriminated render reads it, so adding a type is a data change, not a new
# generator class. Every EnumSubcontractType member MUST have an entry (asserted
# below at import so an unmapped type fails loudly rather than rendering empty).
_CANONICAL_OPERATIONS: dict[EnumSubcontractType, tuple[str, ...]] = {
    EnumSubcontractType.DATABASE: ("connect", "query", "transaction", "disconnect"),
    EnumSubcontractType.API: ("request", "retry", "parse_response"),
    EnumSubcontractType.EVENT: ("subscribe", "handle", "publish"),
    EnumSubcontractType.COMPUTE: ("initialize", "execute_compute", "cleanup"),
    EnumSubcontractType.STATE: ("load", "apply", "persist"),
    EnumSubcontractType.WORKFLOW: ("start", "step", "complete"),
}

_MISSING = [t for t in EnumSubcontractType if t not in _CANONICAL_OPERATIONS]
if _MISSING:  # pragma: no cover - guards against an unmapped enum member
    raise RuntimeError(f"subcontract types without canonical operations: {_MISSING}")


def canonical_operations(subcontract_type: EnumSubcontractType) -> tuple[str, ...]:
    """Return the canonical default operation list for a subcontract type."""

    return _CANONICAL_OPERATIONS[subcontract_type]


def render_subcontract(
    request: ModelSubcontractRenderRequest,
) -> ModelSubcontractFragment:
    """Render one subcontract fragment as deterministic YAML text plus its digest.

    The fragment is a single ``{type: {operations: [...], **extra_fields}}``
    mapping. Operations fall back to the canonical list for the type when the
    request supplies none. The digest is the sha256 of the rendered text.
    """

    operations = (
        list(request.operations)
        if request.operations
        else list(canonical_operations(request.type))
    )
    body: dict[str, Any] = {"operations": operations}
    for key, value in request.extra_fields.items():
        body[key] = value

    fragment = {request.type.value: body}
    yaml_fragment = yaml.safe_dump(fragment, default_flow_style=False, sort_keys=False)
    sha256 = hashlib.sha256(yaml_fragment.encode("utf-8")).hexdigest()
    return ModelSubcontractFragment(
        type=request.type,
        yaml_fragment=yaml_fragment,
        sha256=sha256,
    )


__all__ = ["canonical_operations", "render_subcontract"]
