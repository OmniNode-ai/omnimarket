# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The seams of the capture silence watch's database password reference (OMN-24325).

Installer to node: the unit maps two environment variables to two request fields; the node's contract names the same two variables
(dsn_env and password_ref_env). Every reference the request accepts is accepted by ``split_store_reference`` and every reference it
refuses is refused there too, and a database address that carries a password is refused by the request. A rendered unit holds the
address and the reference and never a password, and an environment built from what the unit persists gives the node the DSN it dials.
Node to the secret store: the node reads ``(folder, key)`` through the reader's ``read`` and the overlay id it loads is the folder name
of the overlay in the overlays repository. Shape followed: tests/nodes/node_source_read_cache_effect/test_password_ref_installer_seam.py.
No bus topic is involved, so no in-memory or real broker run applies; the real readings are the store and the database on the lab host.
"""

from __future__ import annotations

import inspect
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
import yaml
from pydantic import SecretStr, ValidationError

from omnimarket.handlers.delegation_scenario_settings import SecretStoreSettingsReader
from omnimarket.nodes.node_capture_silence_watch_effect.handlers.capture_watch_reader import (
    NODE_NAME,
    resolve_reads_source_dsn,
)
from omnimarket.nodes.node_capture_silence_watch_effect.handlers.load_capture_watch_config import (
    load_capture_watch_config,
)
from omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units import (
    HandlerRegistryUnits,
    load_units,
)
from omnimarket.nodes.node_host_installer_registry_units.models.model_registry_units import (
    ModelRegistryUnitsRequest,
)
from omnimarket.nodes.node_source_read_cache_effect.handlers.password_reference import (
    resolve_password,
    split_store_reference,
)

pytestmark = pytest.mark.unit

UNIT = "capture-silence-watch"
NODE = "node_capture_silence_watch_effect"
ROOT = Path(__file__).resolve().parents[3]
CONTRACT = ROOT / "src" / "omnimarket" / "nodes" / NODE / "contract.yaml"
URL_FIELD = "capture_watch_db_url"
REF_FIELD = "capture_watch_db_password_ref"
URL = "postgresql://omninode_runtime@127.0.0.1:5436/omnidash_analytics"
PREFIX = "secret://onex/"
REFERENCE = PREFIX + "shared/env/OMNINODE_RUNTIME_PASSWORD"
VALID = (
    REFERENCE,
    PREFIX + "K",
    PREFIX + "a/b/K_1.x-y",
)
INVALID = (
    "hunter2",
    PREFIX,
    PREFIX + "a/ b/K",
    PREFIX + "a//K",
    PREFIX + "shared/env/",
    "http://example.invalid/shared/env/K",
)
URLS_REFUSED = (
    "postgresql://omninode_runtime:hunter2@127.0.0.1:5436/omnidash_analytics",
    "postgresql://omninode_runtime@127.0.0.1/omnidash_analytics",
    "mysql://omninode_runtime@127.0.0.1:5436/omnidash_analytics",
    "postgresql://omninode_runtime@127.0.0.1:5436/omnidash analytics",
    "postgresql://127.0.0.1:5436/omnidash_analytics",
)


OVERLAY_ROOT_OVERRIDE = {"ONEX_SKILL_OVERLAY_ROOTS": "/overlays/clone"}


def _no_command(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(argv, 0, "", "")


def _request(tmp_path: Path, **fields: Any) -> ModelRegistryUnitsRequest:
    return ModelRegistryUnitsRequest(
        unit=UNIT, home=str(tmp_path), registry_root=str(tmp_path), **fields
    )


def _watch() -> dict[str, Any]:
    return yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))


def test_the_unit_maps_the_contract_variables_to_the_request_fields() -> None:
    unit = load_units()[UNIT]
    reads = _watch()["reads_source"]
    assert unit["environment_inputs"] == {
        reads["dsn_env"]: URL_FIELD,
        reads["password_ref_env"]: REF_FIELD,
    }
    assert set(unit["required_environment_inputs"]) == set(unit["environment_inputs"])
    assert {URL_FIELD, REF_FIELD} <= set(ModelRegistryUnitsRequest.model_fields)


def test_the_unit_reads_the_overlay_roots_and_refuses_a_worktree_root() -> None:
    unit = load_units()[UNIT]
    assert "ONEX_SKILL_OVERLAY_ROOTS" in unit["environment"]
    assert "ONEX_SKILL_OVERLAY_ROOTS" in unit["environment_path_overrides"]
    assert "ONEX_SKILL_OVERLAY_ROOTS" in unit["environment_refuse_worktree_paths"]


@pytest.mark.parametrize("reference", VALID)
def test_every_reference_the_request_accepts_is_accepted_by_the_node(
    tmp_path: Path, reference: str
) -> None:
    assert (
        _request(tmp_path, **{REF_FIELD: reference}).capture_watch_db_password_ref
        == reference
    )
    split_store_reference(reference)


@pytest.mark.parametrize("reference", INVALID)
def test_every_reference_the_request_refuses_is_refused_by_the_node(
    tmp_path: Path, reference: str
) -> None:
    with pytest.raises(ValidationError):
        _request(tmp_path, **{REF_FIELD: reference})
    with pytest.raises(ValueError, match="secret store reference"):
        split_store_reference(reference)


@pytest.mark.parametrize("url", URLS_REFUSED)
def test_the_request_refuses_an_address_that_carries_a_password_or_is_not_complete(
    tmp_path: Path, url: str
) -> None:
    with pytest.raises(ValidationError):
        _request(tmp_path, **{URL_FIELD: url})


def test_the_installer_refuses_an_install_that_names_neither_input(
    tmp_path: Path,
) -> None:
    handler = HandlerRegistryUnits(_no_command, units=load_units())
    with pytest.raises(ValueError, match="is unset"):
        handler.environment(
            _request(tmp_path, mode="install", path_overrides=OVERLAY_ROOT_OVERRIDE),
            load_units()[UNIT],
        )


class _Store:
    def __init__(self, held: dict[tuple[str, str], str]) -> None:
        self.held = held

    async def read(self, folder: str, name: str) -> SecretStr | None:
        value = self.held.get((folder, name))
        return SecretStr(value) if value is not None else None


def test_what_the_unit_persists_gives_the_node_the_dsn_it_dials(tmp_path: Path) -> None:
    """Installer to node, end to end in process: persisted environment in, the dialled DSN out, the password only in the node."""
    from psycopg2.extensions import parse_dsn

    handler = HandlerRegistryUnits(_no_command, units=load_units())
    request = _request(
        tmp_path,
        mode="install",
        marketplace_root=str(tmp_path / "market"),
        path_overrides=OVERLAY_ROOT_OVERRIDE,
        **{URL_FIELD: URL, REF_FIELD: REFERENCE},
    )
    persisted = handler.environment(request, load_units()[UNIT])
    assert persisted["OMNINODE_INTERNAL_DB_URL"] == URL
    assert persisted["ONEX_CAPTURE_WATCH_DB_PASSWORD_REF"] == REFERENCE
    assert all(urlsplit(value).password is None for value in persisted.values())
    source = load_capture_watch_config(CONTRACT)[1]
    store = _Store({("/shared/env", "OMNINODE_RUNTIME_PASSWORD"): "synthetic-pw"})
    dsn = resolve_reads_source_dsn(
        source,
        environ=persisted,
        resolve=lambda reference, store_overlay=None: resolve_password(
            reference, reader=store, store_overlay=store_overlay
        ),
        load_store=lambda _name: None,
    )
    assert parse_dsn(dsn)["password"] == "synthetic-pw"


def test_the_node_loads_the_overlay_named_like_its_overlay_folder() -> None:
    assert NODE_NAME == NODE


def test_the_secret_store_reader_keeps_the_signature_the_node_calls() -> None:
    read = SecretStoreSettingsReader.read
    assert inspect.iscoroutinefunction(read)
    assert list(inspect.signature(read).parameters) == ["self", "folder", "name"]
