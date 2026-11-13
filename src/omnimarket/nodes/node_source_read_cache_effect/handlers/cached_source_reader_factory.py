# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Build the cached reader a board step uses from the deployment overlay, failing safe to the uncached read.

An absent or invalid overlay, an unset connection variable or an unreachable server never stops a step: the reader then has no
handler and every read goes to its source as it did before the cache existed, and the tally says why (the exception's class name or the
variable names, never a message or a value that could carry deployment facts).
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path

import yaml

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.models.model_source_read_cache import (
    ModelSourceCacheOverlay,
)
from omnimarket.nodes.node_source_read_cache_effect.handlers.backend_memory import (
    BackendMemorySourceReadCache,
)
from omnimarket.nodes.node_source_read_cache_effect.handlers.backend_valkey import (
    BackendValkeySourceReadCache,
    valkey_url,
)
from omnimarket.nodes.node_source_read_cache_effect.handlers.cached_source_reader import (
    CachedSourceReader,
)
from omnimarket.nodes.node_source_read_cache_effect.handlers.handler_source_read_cache import (
    HandlerSourceReadCache,
)
from omnimarket.nodes.node_source_read_cache_effect.protocols.protocol_source_read_cache_backend import (
    ProtocolSourceReadCacheBackend,
)

OWN_CONTRACT = Path(__file__).resolve().parents[1] / "contract.yaml"


def _overlay_wiring(contract_path: Path = OWN_CONTRACT) -> tuple[str, str]:
    runtime = yaml.safe_load(contract_path.read_text(encoding="utf-8"))["runtime"]
    return runtime["overlay"]["overlay_id"], runtime["overlay"]["file_env"]


def write_tally_receipt(
    reader: CachedSourceReader,
    home: Path,
    step: str,
    *,
    now: datetime | None = None,
    contract_path: Path = OWN_CONTRACT,
) -> Path:
    """Write the reader's tally for ``step`` under the registry home, beside the other state the cycle keeps; return the file."""
    runtime = yaml.safe_load(contract_path.read_text(encoding="utf-8"))["runtime"]
    path = home / str(runtime["receipt_dir_relpath"]) / f"{step}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "step": step,
        "written_at": (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        **reader.tally.model_dump(),
    }
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(document, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def build_cached_source_reader(
    environ: Mapping[str, str] | None = None,
    *,
    overlay: ModelSourceCacheOverlay | None = None,
    backend: ProtocolSourceReadCacheBackend | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> CachedSourceReader:
    """The reader for this deployment: the overlay's backend, or a reader with no handler when the overlay cannot be used."""
    env = os.environ if environ is None else environ
    overlay_id, file_env = _overlay_wiring()
    try:
        declared = load_structured_overlay(
            overlay_id,
            ModelSourceCacheOverlay,
            env,
            explicit=overlay,
            file_env=file_env,
        )
    except NodeOverlayError as exc:
        return CachedSourceReader(
            None, clock, disabled_reason=f"overlay: {type(exc).__name__}"
        )
    chosen = backend
    backend_name = "valkey" if declared.valkey is not None else "memory"
    connection = declared.valkey
    if chosen is None and connection is None:
        chosen = BackendMemorySourceReadCache(clock)
    elif chosen is None and connection is not None:
        host = env.get(connection.host_env, "").strip()
        port = env.get(connection.port_env, "").strip()
        if not host or not port.isdigit():
            return CachedSourceReader(
                None,
                clock,
                disabled_reason=(
                    f"valkey: host or port variable not set ({connection.host_env}, {connection.port_env})"
                ),
                backend_name=backend_name,
            )
        password = (
            env.get(connection.password_env, "") if connection.password_env else ""
        )
        chosen = BackendValkeySourceReadCache(
            valkey_url(host, int(port), password or None, connection.database),
            connection.connect_timeout_seconds,
            connection.operation_timeout_seconds,
        )
    handler = HandlerSourceReadCache(
        chosen, declared.key_prefix, declared.ttl_backstop_seconds
    )
    return CachedSourceReader(
        handler,
        clock,
        backend_name=backend_name,
        clone_settings=(
            declared.clone_relpath_template,
            declared.clone_revision_timeout_seconds,
        ),
    )
