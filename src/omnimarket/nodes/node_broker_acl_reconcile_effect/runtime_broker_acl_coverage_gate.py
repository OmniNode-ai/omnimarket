# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The merge check's entry point: a topic use this change adds to a node contract needs a declared broker grant (OMN-24005).

uv run python -m omnimarket.nodes.node_broker_acl_reconcile_effect.runtime_broker_acl_coverage_gate --base-ref origin/dev
Exit 0 covered, 1 refused (each uncovered use named), 2 the base or the contracts could not be read.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from omnimarket.models.broker_acl import (
    EnumAclResourceType,
    ModelBrokerAclCoverageRequest,
    ModelBrokerAclRow,
)
from omnimarket.nodes.node_broker_acl_overlay_render_compute.handlers.handler_broker_acl_coverage_gate import (
    HandlerBrokerAclCoverageGate,
)
from omnimarket.nodes.node_broker_acl_reconcile_effect.handlers.contract_topics import (
    load_topic_uses,
)
from omnimarket.nodes.node_broker_acl_reconcile_effect.handlers.contract_topics_at_ref import (
    load_topic_uses_at_ref,
)
from omnimarket.nodes.node_broker_acl_reconcile_effect.handlers.handler_broker_acl_reconcile import (
    aggregate_declared_grants,
)

_NODES_DIR = "src/omnimarket/nodes"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-ref", required=True, help="the base branch's revision")
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    nodes_root = args.repo_root / _NODES_DIR
    try:
        request = ModelBrokerAclCoverageRequest(
            topic_uses=load_topic_uses(nodes_root),
            base_topic_uses=load_topic_uses_at_ref(
                args.repo_root, args.base_ref, _NODES_DIR
            ),
            declared_rows=tuple(
                ModelBrokerAclRow(**row)
                for row in aggregate_declared_grants(nodes_root)
                if row["resource_type"] == EnumAclResourceType.TOPIC.value
            ),
        )
    except (ValueError, OSError) as error:
        sys.stderr.write(f"broker permission coverage: {error}\n")
        return 2
    result = HandlerBrokerAclCoverageGate().handle(request)
    for finding in result.findings:
        sys.stdout.write(f"{finding.code.value}: {finding.detail}\n")
    sys.stdout.write(
        f"broker permission coverage: {result.status}; {result.new_uses} topic uses added since {args.base_ref}, {result.uncovered_new_uses} with no declared grant\n"
    )
    return 0 if result.status == "covered" else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main"]
