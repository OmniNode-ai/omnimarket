# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed rows for the inputs two lane-facing readers need on a lab host.

``onex-capability-catalog find`` reads a catalog.json named by ``--in`` or ``ONEX_CAPABILITY_CATALOG_JSON``; the work-ledger projection
parity check reads its database password from the lab env file named by ``WL_ENV_FILE`` (default ``~/.omnibase/.env``). A lane shell has
neither variable set by hand, so the host must provision both. A row carries paths and key NAMES only, never a value.
"""

from pydantic import BaseModel, ConfigDict


class ModelLaneReaderHost(BaseModel):
    """One declared lane host. ``catalog_json`` and ``wl_env_file`` are the paths its lanes are given (relative to the lane's home).

    ``wl_env_key_names`` and ``catalog_json_present`` are readings of the host, not declarations: ``None`` means not measured and is
    never a failure; a tuple lists the key NAMES the env file holds.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str
    catalog_json: str | None = None
    wl_env_file: str | None = None
    wl_env_key_names: tuple[str, ...] | None = None
    catalog_json_present: bool | None = None


class ModelLaneReaderFailure(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str
    reader: str
    item: str
    reason: str


class ModelLaneReaderInputsRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    hosts: tuple[ModelLaneReaderHost, ...]


class ModelLaneReaderInputsResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    # In host order, then reader order; empty when every host has every input.
    failures: tuple[ModelLaneReaderFailure, ...]
