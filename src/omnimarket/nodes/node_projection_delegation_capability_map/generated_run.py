# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Build an offline capability-map event from fresh, pinned generations.

Only classes with an executable rubric contribute. Incomplete generations and
undetermined rubric outcomes cannot establish a pass. Historical answer labels
are never used. The existing projection writer persists the resulting event.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from omnimarket.delegation.rubric.attempt_verdict import record_attempt_rubric_verdict
from omnimarket.delegation.rubric.contract_loader import load_delegation_class_rubrics
from omnimarket.inference.task_class_authority import load_task_class_authority
from omnimarket.lab_work.named_env import read_named_env
from omnimarket.models.delegation.model_size_band import ModelSizeBand
from omnimarket.models.delegation.wire.model_attempt_configuration import (
    ModelAttemptConfiguration,
)
from omnimarket.models.delegation_capability_map.enum_capability_source import (
    EnumCapabilitySource,
)
from omnimarket.models.delegation_capability_map.model_capability_configuration import (
    ModelCapabilityConfiguration,
)
from omnimarket.models.delegation_capability_map.model_capability_identity import (
    ModelCapabilityIdentity,
)
from omnimarket.models.delegation_capability_map.model_capability_map_run_recorded import (
    ModelCapabilityMapRunRecorded,
)
from omnimarket.models.delegation_capability_map.model_capability_serving_assignment import (
    ModelCapabilityServingAssignment,
)
from omnimarket.models.delegation_capability_map.model_capability_verdict_row import (
    ModelCapabilityVerdictRow,
)
from omnimarket.models.delegation_eval_backend import ModelDelegationEvalBackend
from omnimarket.nodes.node_projection_delegation_capability_map.handlers.handler_delegation_capability_map_writer import (
    HandlerDelegationCapabilityMapWriter,
)
from omnimarket.nodes.node_projection_delegation_capability_map.lab_run import _band


def build_generated_map_run(
    tenant_id: UUID,
    items: Sequence[dict[str, Any]],
    backend: ModelDelegationEvalBackend,
    manifest_id: str,
    configuration: ModelCapabilityConfiguration,
) -> tuple[ModelCapabilityMapRunRecorded, dict[str, int]]:
    """Fresh rubric evidence under the attested serving assignment, never blind labels."""
    contract = load_delegation_class_rubrics()
    authority = load_task_class_authority()
    skipped: Counter[str] = Counter()
    model_key = backend.model
    identity = ModelCapabilityIdentity(
        model_revision=backend.model_file,
        backend=backend.model_dump_json(),
        inference_configuration=configuration.configuration_id,
        suite_revision=manifest_id,
        grader_revision=contract.rubric_version,
        harness_revision="delegation-backend-run.v1",
    )
    rows = []
    seen = set()
    for item in items:
        if item["backend_configuration"] != backend.model_dump(mode="json"):
            raise ValueError("generated item backend differs from pin")
        if item["item_key"] in seen:
            raise ValueError("duplicate generated item key")
        seen.add(item["item_key"])
        task_class = item["task_class"]
        if task_class not in contract.classes:
            skipped["class_rubric_unavailable"] += 1
            continue
        band = _band(authority, task_class, item["prompt_snapshot"])
        if not isinstance(band, ModelSizeBand):
            skipped[f"size_band_{band}"] += 1
            continue
        passed = False
        if not item.get("error") and item.get("finish_reason") == "stop":
            verdict = record_attempt_rubric_verdict(
                task_class=task_class,
                request_text=item["prompt_snapshot"],
                answer_text=item["response_snapshot"] or "",
            )
            passed = verdict.outcome == "PASS"
        rows.append(
            ModelCapabilityVerdictRow(
                tenant_id=tenant_id,
                item_key=item["item_key"],
                source=EnumCapabilitySource.GRID_RUN,
                task_class=task_class,
                size_band=band.band,
                model_key=model_key,
                backend_id=backend.backend_id,
                configuration=configuration,
                identity=identity,
                passed=passed,
            )
        )
    digest = hashlib.sha256(
        json.dumps(
            {
                "items": sorted(items, key=lambda item: item["item_key"]),
                "backend": backend.model_dump(mode="json"),
                "configuration": configuration.model_dump(mode="json"),
                "suite": manifest_id,
                "grader": contract.rubric_version,
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    return ModelCapabilityMapRunRecorded(
        tenant_id=tenant_id,
        map_run_id=str(uuid5(NAMESPACE_URL, f"generated-capability:{digest}")),
        observed_at=datetime.now(UTC),
        rows=tuple(rows),
        assignments=(
            ModelCapabilityServingAssignment(
                model_key=model_key,
                backend_id=backend.backend_id,
                configuration_id=configuration.configuration_id,
                identity=identity,
            ),
        ),
    ), dict(skipped)


def main(argv: list[str] | None = None) -> int:
    """Persist fresh evidence only through the existing projection writer."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--items", type=Path, required=True)
    parser.add_argument("--backend-pin", type=Path, required=True)
    parser.add_argument("--manifest-id", required=True)
    parser.add_argument("--write-dsn-env", required=True)
    parser.add_argument("--max-tokens", type=int, required=True)
    args = parser.parse_args(argv)
    items = [
        json.loads(line) for line in args.items.read_text().splitlines() if line.strip()
    ]
    backend = ModelDelegationEvalBackend.model_validate_json(
        args.backend_pin.read_text()
    )
    configuration = ModelCapabilityConfiguration(
        parameters=ModelAttemptConfiguration(
            max_tokens=args.max_tokens, temperature=0, reasoning_effort="disabled"
        )
    )
    run, skipped = build_generated_map_run(
        UUID(args.tenant_id), items, backend, args.manifest_id, configuration
    )
    writer = HandlerDelegationCapabilityMapWriter()
    writer.bind_projection_database_url(read_named_env(args.write_dsn_env))
    receipt = writer.handle(run.model_dump(mode="json"))
    sys.stdout.write(json.dumps({"writer": receipt, "skipped": skipped}) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
