# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared fixtures of the ledger file census suites (OMN-22917)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import yaml

from omnimarket.handlers.emission_sink import InMemoryEmissionSink
from omnimarket.nodes.node_work_ledger_file_census_effect.adapters import (
    InMemoryRatchetStore,
    InMemoryRepositorySource,
)
from omnimarket.nodes.node_work_ledger_file_census_effect.handlers import (
    HandlerFileCensus,
)
from omnimarket.nodes.node_work_ledger_file_census_effect.models import (
    ModelCensusOverlay,
)
from omnimarket.nodes.node_work_ledger_file_census_effect.protocols import (
    ProtocolRepositorySource,
)

PACKAGE = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_work_ledger_file_census_effect"
)
NOW = datetime(2026, 11, 7, 15, 0, tzinfo=UTC)

CLEAN = {
    "src/a.py": '"""Cites ROLLING_WORK_LEDGER.md:3952 and ONEX_LEDGER_PATH in prose."""\n'
    "# reads ROLLING_WORK_LEDGER here? no: a comment\nvalue = 1\n",
    "docs/ruling.md": "see ROLLING_WORK_LEDGER.md:5988 and ONEX_LEDGER_PATH\n",
    "run.sh": "# cat $ONEX_LEDGER_PATH\necho done  # ROLLING_WORK_LEDGER\n"
    "echo 'see ROLLING_WORK_LEDGER.md:17'\n",
}
READER = 'import os\npath = os.environ["ONEX_LEDGER_PATH"]\n'
WRITER = 'open("ROLLING_WORK_LEDGER.md", "a").write(row)\n'


def overlay() -> ModelCensusOverlay:
    data = yaml.safe_load((PACKAGE / "overlay.example.yaml").read_text())
    data["repositories"] = [
        {"name": "alpha", "own_line": False},
        {"name": "beta", "own_line": False},
        {"name": "outside", "own_line": True},
    ]
    return ModelCensusOverlay.model_validate(data)


def repositories(**extra: dict[str, str]) -> dict[str, dict[str, str]]:
    base = {"alpha": dict(CLEAN), "beta": {"x.py": "x = 1\n"}, "outside": {}}
    base.update(extra)
    return base


def handler(
    repos: dict[str, dict[str, str]],
    ratchet: dict[str, int] | None,
    sink: InMemoryEmissionSink | None = None,
    source: ProtocolRepositorySource | None = None,
) -> tuple[HandlerFileCensus, InMemoryEmissionSink]:
    out = sink or InMemoryEmissionSink()
    return (
        HandlerFileCensus(
            overlay(),
            source or InMemoryRepositorySource(repos),
            InMemoryRatchetStore(ratchet),
            out,
            clock=lambda: NOW,
            backend="in_memory",
        ),
        out,
    )
