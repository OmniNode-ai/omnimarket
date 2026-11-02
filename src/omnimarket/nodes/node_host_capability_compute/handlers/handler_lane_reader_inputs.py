# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Judge whether a declared lane host provisions the inputs of the catalog ``find`` reader and the ledger parity check.

A lane on a lab host measured 2026-10-31 (and again 2026-11-02) could run neither reader: ``onex-capability-catalog find`` answered
"no catalog.json given" because ``ONEX_CAPABILITY_CATALOG_JSON`` is set nowhere, and the parity check had no database password because
the lab env file held no ``POSTGRES_PASSWORD``. Both inputs belong to the host installer; this node holds the requirement and names, for
each host that lacks one, what the owner adds. It reads paths and key names only and never a value.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from ..models.model_lane_reader_inputs import (
    ModelLaneReaderFailure,
    ModelLaneReaderHost,
    ModelLaneReaderInputsRequest,
    ModelLaneReaderInputsResult,
)

CATALOG_FIND = "catalog-find"
LEDGER_PARITY = "ledger-parity"
CATALOG_ENV = "ONEX_CAPABILITY_CATALOG_JSON"
WL_ENV = "WL_ENV_FILE"
# The key the parity check builds its database DSN from (ledger-ask, step 2b); it is read from the env file and unset after use.
WL_PASSWORD_KEY = "POSTGRES_PASSWORD"
TABLE_PATH = Path(__file__).resolve().parents[1] / "lane_reader_inputs.yaml"

OWNER_FIX = {
    CATALOG_FIND: f"`onex-capability-catalog find` reads --in or {CATALOG_ENV} and has no default, so a lane with neither answers 'no catalog.json given'; "
    f"the host installer gives the host a catalog.json and exports {CATALOG_ENV} to lanes",
    LEDGER_PARITY: f"the work-ledger parity check reads its database password from {WL_ENV} (default ~/.omnibase/.env); "
    f"the host installer names the file for lanes, and the host owner supplies the key {WL_PASSWORD_KEY} in it (this node never holds a value)",
}


def parse_declared_hosts(text: str) -> tuple[ModelLaneReaderHost, ...]:
    """The declared lane hosts of a table's text (the caller reads the file: this node does no I/O)."""
    raw = yaml.safe_load(text) or {}
    rows = raw.get("hosts") or {}
    return tuple(
        ModelLaneReaderHost(host=name, **(fields or {}))
        for name, fields in rows.items()
    )


def _failures(host: ModelLaneReaderHost) -> list[ModelLaneReaderFailure]:
    found: list[ModelLaneReaderFailure] = []

    def fail(reader: str, item: str, why: str) -> None:
        found.append(
            ModelLaneReaderFailure(
                host=host.host,
                reader=reader,
                item=item,
                reason=f"{why}: {OWNER_FIX[reader]}",
            )
        )

    if not host.catalog_json:
        fail(CATALOG_FIND, CATALOG_ENV, "no catalog.json path is declared for the host")
    elif host.catalog_json_present is False:
        fail(CATALOG_FIND, host.catalog_json, "the declared catalog.json is absent")
    if not host.wl_env_file:
        fail(LEDGER_PARITY, WL_ENV, "no lab env file is declared for the host")
    elif (
        host.wl_env_key_names is not None
        and WL_PASSWORD_KEY not in host.wl_env_key_names
    ):
        fail(
            LEDGER_PARITY,
            WL_PASSWORD_KEY,
            f"the env file holds no {WL_PASSWORD_KEY} key",
        )
    return found


class HandlerLaneReaderInputs:
    def handle(
        self, request: ModelLaneReaderInputsRequest
    ) -> ModelLaneReaderInputsResult:
        return ModelLaneReaderInputsResult(
            failures=tuple(f for h in request.hosts for f in _failures(h))
        )
