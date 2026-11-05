# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The old ledger names remain links to the configured ledger on the host that holds it.

If a restore in the old clone makes an old name a regular file again, a writer still configured
with that name writes one file while readers of the new name read another, and nothing else sees
it. Named break: replace an old name with a regular file.

REMOVABLE. The ledger's move to the event bus ends every file ledger, and this probe then has
nothing to read. At that cutover delete this file, its test file, the ``old_names`` section of the
overlay and its overlay model, ``LEDGER_OLD_NAMES`` in ``EnumHostProbe`` and the two branches that
name it in the handler; no other code reads any of them.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from omnimarket.models.model_host_reading import EnumHostProbe, ModelProbeReading
from omnimarket.models.model_reading import EnumReadingOutcome, EnumReadingVerdict
from omnimarket.nodes.node_host_reading_effect.overlay import ModelOldNamesProbeSettings
from omnimarket.nodes.node_host_reading_effect.protocols import ProtocolHostFiles
from omnimarket.nodes.node_reading_compute.handlers.handler_reading_compute import (
    reason_token,
)

_PROBE = EnumHostProbe.LEDGER_OLD_NAMES


def _unknown(reason: str) -> ModelProbeReading:
    return ModelProbeReading(
        probe=_PROBE,
        verdict=EnumReadingVerdict.UNKNOWN,
        outcome=EnumReadingOutcome.SOURCE_UNREADABLE,
        reasons=(reason,),
    )


def read_old_names(
    settings: ModelOldNamesProbeSettings,
    files: ProtocolHostFiles,
    environ: Mapping[str, str],
) -> ModelProbeReading:
    root_text = environ.get(settings.root_env, "").strip()
    configured_text = environ.get(settings.ledger_path_env, "").strip()
    if not root_text:
        return _unknown(f"root-unset-{settings.root_env.lower()}")
    if not configured_text:
        return _unknown(f"configured-path-unset-{settings.ledger_path_env.lower()}")
    root = Path(root_text)
    configured = Path(configured_text)
    reasons: list[str] = []
    configured_kind = files.kind(configured)
    if configured_kind == "link":
        reasons.append("configured-path-is-a-link")
    elif configured_kind != "file":
        reasons.append("configured-path-missing")
    for link in settings.links:
        token = reason_token(link.name)
        old = root / link.name
        kind = files.kind(old)
        if kind == "missing":
            reasons.append(f"old-name-missing-{token}")
        elif kind != "link":
            reasons.append(f"old-name-not-a-link-{token}")
        elif files.resolve(old) != files.resolve(
            configured.parent / link.ledger_relative
        ):
            reasons.append(f"old-name-wrong-target-{token}")
    return ModelProbeReading(
        probe=_PROBE,
        verdict=EnumReadingVerdict.NOT_WORKING
        if reasons
        else EnumReadingVerdict.WORKING,
        outcome=EnumReadingOutcome.READ,
        reasons=tuple(reasons) or ("old-names-link-to-the-configured-ledger",),
        cells={"names": str(len(settings.links))},
    )
