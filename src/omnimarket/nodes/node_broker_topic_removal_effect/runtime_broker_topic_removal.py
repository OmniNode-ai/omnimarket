# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_broker_topic_removal_effect <verb>``.

``plan``    a dry run: classifies every topic, lists the removal candidates with their evidence and the
            partitions the broker would hold afterwards; changes nothing.
``execute`` the same plan, then removes the candidates; the runtime wires no remover, so it is refused
            with ``remover_not_wired`` until a real one is wired.

Exit codes: 0 the run planned or removed; 1 the run was refused; 3 a deployment fact is missing
(overlay, env variable).
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_broker_topic_removal_effect.adapters.declared_topics import (
    ContractFileDeclaredTopics,
)
from omnimarket.nodes.node_broker_topic_removal_effect.adapters.topic_inventory import (
    SshPublicMetricsInventory,
)
from omnimarket.nodes.node_broker_topic_removal_effect.handlers.handler_broker_topic_removal import (
    HandlerBrokerTopicRemoval,
)
from omnimarket.nodes.node_broker_topic_removal_effect.models.model_broker_topic_removal import (
    EnumRemovalMode,
    ModelTopicRemovalCommand,
)
from omnimarket.nodes.node_broker_topic_removal_effect.overlay import (
    NODE_NAME,
    ModelTopicRemovalOverlay,
    load_overlay,
)
from omnimarket.nodes.node_broker_topic_removal_effect.protocols import (
    DeclaredTopicsError,
)


def _required(name: str, environ: Mapping[str, str]) -> str:
    value = environ.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} is not set; the {NODE_NAME} overlay names it")
    return value


def build_handler(
    overlay: ModelTopicRemovalOverlay, environ: Mapping[str, str]
) -> HandlerBrokerTopicRemoval:
    _required(overlay.metrics_source.ssh_host_env, environ)
    _required(overlay.contracts.registry_root_env, environ)
    return HandlerBrokerTopicRemoval(
        overlay=overlay,
        inventory=SshPublicMetricsInventory(overlay.metrics_source, environ),
        declared=ContractFileDeclaredTopics(overlay.contracts, environ),
        remover=None,
    )


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        parser = argparse.ArgumentParser(prog=NODE_NAME, description=__doc__)
        verbs = parser.add_subparsers(dest="verb", required=True)
        plan = verbs.add_parser("plan", help="a dry run; changes nothing")
        plan.add_argument("--out", help="write the result JSON to this path")
        execute = verbs.add_parser("execute", help="remove the candidates")
        execute.add_argument(
            "--approval-ref", required=True, help="the operator's go (a ledger row id)"
        )
        execute.add_argument("--out", help="write the result JSON to this path")
        args = parser.parse_args(arguments)
        overlay = load_overlay()
        handler = build_handler(overlay, os.environ)
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        mode = (
            EnumRemovalMode.DRY_RUN if args.verb == "plan" else EnumRemovalMode.EXECUTE
        )
        result = handler.handle(
            ModelTopicRemovalCommand(
                correlation_id=f"broker-topic-removal-{now}",
                mode=mode,
                approval_ref=getattr(args, "approval_ref", None),
            )
        )
        if args.out:
            Path(args.out).write_text(
                result.model_dump_json(indent=2), encoding="utf-8"
            )
        for entry in result.classes:
            sys.stdout.write(
                f"CLASS {entry.topic_class.value} topics={entry.topics} "
                f"partitions={entry.partitions} produced_bytes={entry.produced_bytes}\n"
            )
        sys.stdout.write(
            f"SUMMARY topics_before={result.topics_before} "
            f"partitions_before={result.partitions_before} "
            f"candidates={len(result.candidates)} "
            f"candidate_partitions={result.candidate_partitions} "
            f"partitions_after={result.partitions_after} "
            f"over_run_cap={'true' if result.over_run_cap else 'false'} "
            f"status={result.status} "
            f"refusal={','.join(result.refusal_reasons) or '-'}\n"
        )
        return 1 if result.status == "refused" else 0
    except (
        NodeOverlayError,
        DeclaredTopicsError,
        ValidationError,
        ValueError,
        OSError,
    ) as exc:
        sys.stderr.write(f"INDETERMINATE: {type(exc).__name__}: {exc}\n")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
