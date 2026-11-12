# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Standalone runtime for node_dev_roll_failure_capture_effect (OMN-24013).

Reads one JSON event on stdin, dispatches it through the node's handlers,
and prints one JSON line of topic and body. Exits 3 when the overlay is absent.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from omnimarket.nodes.node_dev_roll_failure_capture_effect.adapters.adapter_filesystem import (
    FilesystemRollArtifactStore,
)
from omnimarket.nodes.node_dev_roll_failure_capture_effect.handlers.handler_dev_roll_incident_capture import (
    HandlerDevRollIncidentCapture,
)
from omnimarket.nodes.node_dev_roll_failure_capture_effect.handlers.handler_dev_roll_manifest_record import (
    HandlerDevRollManifestRecord,
)
from omnimarket.nodes.node_dev_roll_failure_capture_effect.models import (
    ModelDevRollIncident,
    ModelDevRollManifest,
)
from omnimarket.nodes.node_dev_roll_failure_capture_effect.overlay import (
    ModelDevRollCaptureSettings,
    load_settings,
)

TOPIC_INCIDENT = "onex.evt.omnimarket.dev-roll-incident.v1"
TOPIC_MANIFEST = "onex.evt.omnimarket.dev-roll-manifest.v1"
TOPIC_TERMINAL = "onex.evt.omnimarket.dev-roll-terminal-recorded.v1"
TOPIC_CAPTURE_FAILED = "onex.evt.omnimarket.dev-roll-capture-failed.v1"
TOPIC_MANIFEST_RECORDED = "onex.evt.omnimarket.dev-roll-manifest-recorded.v1"


def main() -> None:
    settings = load_settings()
    if settings is None:
        print("node_dev_roll_failure_capture_effect: overlay absent", file=sys.stderr)
        sys.exit(3)

    store = FilesystemRollArtifactStore(
        root=Path(settings.artifact_root),
        retention=timedelta(days=settings.retention_days),
        clock=datetime.now,
    )

    incident_handler = HandlerDevRollIncidentCapture(store=store, settings=settings)
    manifest_handler = HandlerDevRollManifestRecord(store=store)

    raw = sys.stdin.read().strip()
    if not raw:
        print("node_dev_roll_failure_capture_effect: no input", file=sys.stderr)
        sys.exit(1)

    event = json.loads(raw)
    topic: str = event.get("topic", "")
    body: dict[str, Any] = event.get("body", {})

    if topic == TOPIC_INCIDENT:
        incident = ModelDevRollIncident(**body)
        result = incident_handler.handle(incident)
        out_topic = TOPIC_TERMINAL if result.__class__.__name__ == "ModelDevRollTerminalRecorded" else TOPIC_CAPTURE_FAILED
    elif topic == TOPIC_MANIFEST:
        manifest = ModelDevRollManifest(**body)
        result = manifest_handler.handle(manifest)
        out_topic = TOPIC_MANIFEST_RECORDED if result.__class__.__name__ == "ModelDevRollManifestRecorded" else TOPIC_CAPTURE_FAILED
    else:
        print(f"node_dev_roll_failure_capture_effect: unknown topic {topic!r}", file=sys.stderr)
        sys.exit(1)

    out = {"topic": out_topic, "body": result.model_dump(mode="json")}
    print(json.dumps(out))


if __name__ == "__main__":
    main()
