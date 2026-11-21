# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read the declared service logins that the writer topics are provisioned for.

Two declarations, both under the overlay roots (the overlays clone):

* the service writers overlay of a contract that declares ``writer_topics`` and a ``service_writers``
  policy: ``<root>/<policy.overlay_id>/overlay.yaml`` (schema ``service_writers.v1``), read with the
  same parser the work ledger serve uses;
* the broker ACL scope overlays, ``<root>/broker_acls_*/overlay.yaml``: a principal's WRITE grants on
  literal topics. The plan keeps one only when it is WRITE on the topic the template composes for it.

Read only; nothing is written and no credential is read.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import yaml

from omnimarket.handlers.work_ledger_consume.service_writers import (
    ModelServiceWritersPolicy,
    service_writers,
)
from omnimarket.models.topic_provision.model_topic_provision_write_grant import (
    ModelTopicProvisionWriteGrant,
)
from omnimarket.models.topic_provision.model_writer_topics_template import (
    ModelWriterTopicsTemplate,
)

ACL_SCOPE_GLOB = "broker_acls_*/overlay.yaml"
PRINCIPAL_PREFIX = "User:"


def _load_yaml(path: Path) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ValueError(f"declaration {path} is unreadable ({exc})") from exc


def read_service_writer_logins(
    contract_roots: Sequence[Path], overlay_roots: Sequence[Path]
) -> tuple[str, ...]:
    """The logins of every service writers overlay a writer_topics contract names, sorted unique."""
    logins: set[str] = set()
    for root in contract_roots:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("contract.yaml")):
            data = _load_yaml(path)
            if not isinstance(data, dict):
                continue
            if "writer_topics" not in data or "service_writers" not in data:
                continue
            policy = ModelServiceWritersPolicy.model_validate(data["service_writers"])
            pattern = ModelWriterTopicsTemplate.model_validate(
                data["writer_topics"]
            ).principal_pattern
            for overlay_root in overlay_roots:
                overlay = overlay_root / policy.overlay_id / "overlay.yaml"
                if overlay.is_file():
                    logins |= service_writers(
                        overlay.read_text(encoding="utf-8"), policy, pattern
                    )
                    break
    return tuple(sorted(logins))


def read_acl_write_grants(
    overlay_roots: Sequence[Path],
) -> tuple[ModelTopicProvisionWriteGrant, ...]:
    """Every WRITE grant on a literal topic in the broker ACL scope overlays, sorted unique."""
    grants: set[ModelTopicProvisionWriteGrant] = set()
    for root in overlay_roots:
        for path in sorted(root.glob(ACL_SCOPE_GLOB)):
            data = _load_yaml(path)
            if not isinstance(data, dict):
                continue
            for entry in data.get("principals") or []:
                name = str(entry.get("principal", "")).removeprefix(PRINCIPAL_PREFIX)
                for grant in entry.get("grants") or []:
                    if (
                        name
                        and grant.get("resource_type") == "TOPIC"
                        and grant.get("pattern") == "LITERAL"
                        and grant.get("operation") == "WRITE"
                    ):
                        grants.add(
                            ModelTopicProvisionWriteGrant(
                                principal=name, topic=str(grant["name"])
                            )
                        )
    return tuple(sorted(grants, key=lambda g: (g.principal, g.topic)))
