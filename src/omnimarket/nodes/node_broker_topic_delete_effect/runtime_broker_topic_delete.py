# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_broker_topic_delete_effect run --command-file <json>``.

``run`` reads the command JSON (``ModelTopicDeleteCommand``), or builds it from a removal plan's result
JSON (``--plan-file``: every candidate becomes a target, nothing else is added), loads the overlay, re-reads the
broker's live evidence and the contracts' declarations, and refuses or deletes per the overlay; it
prints one ``RESULT`` line and one ``ENTRY`` line per named topic, and writes the result JSON to
``--out`` when given.

Exit codes: 0 the run did not fail; 1 the run was refused or a delete failed; 3 a deployment fact is
missing (overlay, env variable, broker admin).
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
from omnimarket.models.model_topic_removal import (
    ModelTopicRemovalResult,
)
from omnimarket.nodes.node_broker_topic_delete_effect.handlers.handler_broker_topic_delete import (
    HandlerBrokerTopicDelete,
)
from omnimarket.nodes.node_broker_topic_delete_effect.models.model_topic_delete import (
    ModelDeleteTarget,
    ModelTopicDeleteCommand,
)
from omnimarket.nodes.node_broker_topic_delete_effect.overlay import (
    NODE_NAME,
    ModelTopicDeleteOverlay,
    load_overlay,
)
from omnimarket.nodes.node_broker_topic_delete_effect.protocols import (
    BrokerAdminError,
    ProtocolBrokerTopicAdmin,
)
from omnimarket.nodes.node_broker_topic_removal_effect.adapters.declared_topics import (
    ContractFileDeclaredTopics,
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
    overlay: ModelTopicDeleteOverlay, environ: Mapping[str, str]
) -> HandlerBrokerTopicDelete:
    from omnimarket.nodes.node_broker_topic_delete_effect.adapters.kafka_admin import (
        KafkaBrokerTopicAdmin,
    )
    from omnimarket.nodes.node_broker_topic_delete_effect.adapters.rpk_admin import (
        RpkSuperuserTopicAdmin,
    )

    _required(overlay.contracts.registry_root_env, environ)
    admin: ProtocolBrokerTopicAdmin = (
        RpkSuperuserTopicAdmin(overlay, environ)
        if overlay.superuser is not None
        else KafkaBrokerTopicAdmin(overlay)
    )
    return HandlerBrokerTopicDelete(
        overlay=overlay,
        declared=ContractFileDeclaredTopics(overlay.contracts, environ),
        admin=admin,
        clock=lambda: datetime.now(UTC),
    )


def command_from_plan(
    plan: ModelTopicRemovalResult,
    *,
    approval_ref: str | None,
    ruling_covers_declared: bool,
    ruling_ref: str | None,
) -> ModelTopicDeleteCommand:
    """The delete command that names exactly the candidates a removal plan returned."""
    return ModelTopicDeleteCommand(
        correlation_id=plan.correlation_id,
        topics=tuple(
            ModelDeleteTarget(name=candidate.name, topic_class=candidate.topic_class)
            for candidate in plan.candidates
        ),
        approval_ref=approval_ref,
        ruling_covers_declared=ruling_covers_declared,
        ruling_ref=ruling_ref,
    )


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        parser = argparse.ArgumentParser(prog=NODE_NAME, description=__doc__)
        run = parser.add_subparsers(dest="verb", required=True).add_parser(
            "run", help="run the delete the command file names"
        )
        source = run.add_mutually_exclusive_group(required=True)
        source.add_argument("--command-file", help="path to the command JSON")
        source.add_argument(
            "--plan-file", help="path to a removal plan's result JSON (its candidates)"
        )
        run.add_argument("--approval-ref", help="the operator's go (a ledger row id)")
        run.add_argument(
            "--ruling-ref", help="the ruling row that covers declared topics"
        )
        run.add_argument("--out", help="write the result JSON to this path")
        args = parser.parse_args(arguments)
        if args.command_file:
            command = ModelTopicDeleteCommand.model_validate_json(
                Path(args.command_file).read_text(encoding="utf-8")
            )
        else:
            command = command_from_plan(
                ModelTopicRemovalResult.model_validate_json(
                    Path(args.plan_file).read_text(encoding="utf-8")
                ),
                approval_ref=args.approval_ref,
                ruling_covers_declared=bool(args.ruling_ref),
                ruling_ref=args.ruling_ref,
            )
        overlay = load_overlay()
        handler = build_handler(overlay, os.environ)
        result = handler.handle(command)
        if args.out:
            Path(args.out).write_text(
                result.model_dump_json(indent=2), encoding="utf-8"
            )
        sys.stdout.write(
            f"RESULT status={result.status} deletes_issued={result.deletes_issued} "
            f"refusal={','.join(result.refusal_reasons) or '-'}\n"
        )
        for entry in result.entries:
            sys.stdout.write(
                f"ENTRY {entry.name} {entry.status.value} "
                f"{','.join(entry.reasons) or '-'}\n"
            )
        return 1 if result.failed else 0
    except (
        BrokerAdminError,
        DeclaredTopicsError,
        NodeOverlayError,
        OSError,
        ValidationError,
        ValueError,
    ) as exc:
        sys.stderr.write(f"INDETERMINATE: {type(exc).__name__}: {exc}\n")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
