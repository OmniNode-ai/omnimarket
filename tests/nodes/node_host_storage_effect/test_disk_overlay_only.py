# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC3 (OMN-23867): every root, command, timeout and host comes from the overlay; the effect has no path default."""

from __future__ import annotations

import ast
import copy
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import (
    OVERLAY_ROOTS_ENV,
    NodeOverlayError,
)
from omnimarket.nodes.node_host_storage_effect.handlers.handler_host_storage import (
    HandlerHostStorage,
)
from omnimarket.nodes.node_host_storage_effect.models.model_host_storage_overlay import (
    ModelHostStorageOverlay,
)

from .disk_inputs import (
    NODE_DIR,
    Dirs,
    by_entry,
    make_dirs,
    make_entry,
    overlay_document,
    real_handler,
    request,
    write_overlay_root,
)

pytestmark = pytest.mark.unit


def _mutated(dirs: Dirs, change: Any) -> dict[str, Any]:
    document = copy.deepcopy(overlay_document(dirs))
    change(document)
    return document


def test_with_no_overlay_root_the_handler_refuses_to_start_and_removes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(OVERLAY_ROOTS_ENV, raising=False)
    dirs = make_dirs(tmp_path)
    entry = make_entry(dirs.cache, "pkg-a")
    with pytest.raises(NodeOverlayError):
        HandlerHostStorage().handle(request("evict_cache", entry))
    assert entry.exists()


def test_the_handler_reads_the_overlay_file_under_the_overlay_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dirs = make_dirs(tmp_path)
    entry = make_entry(dirs.cache, "pkg-a")
    root = write_overlay_root(tmp_path, overlay_document(dirs))
    monkeypatch.setenv(OVERLAY_ROOTS_ENV, str(root))
    result = HandlerHostStorage().handle(request("evict_cache", entry))
    assert by_entry(result) == {str(entry): "removed"}
    assert not entry.exists()


def test_the_roots_are_the_overlays_and_a_different_overlay_moves_them(
    tmp_path: Path,
) -> None:
    dirs = make_dirs(tmp_path)
    entry = make_entry(dirs.cache, "pkg-a")
    moved = overlay_document(dirs)
    moved["hosts"][0]["entry_roots"][0]["path"] = str(dirs.lab_runs)
    overlay = ModelHostStorageOverlay.model_validate(moved)
    from omnimarket.nodes.node_host_storage_effect.handlers.handler_host_storage import (
        select_backend,
    )

    result = HandlerHostStorage(overlay, select_backend(overlay)).handle(
        request("evict_cache", entry)
    )
    assert by_entry(result) == {str(entry): "refused_outside_root"}
    assert entry.exists()
    assert (
        real_handler(dirs, no_holders=True)
        .handle(request("evict_cache", entry))
        .entry_results[0]
        .outcome
        == "removed"
    )


@pytest.mark.parametrize(
    "change",
    [
        # an entry command listed for a host with no root declared for it: no default path
        lambda d: d["hosts"][0].update(
            entry_roots=[
                r for r in d["hosts"][0]["entry_roots"] if r["command"] != "evict_cache"
            ]
        ),
        # a relative root
        lambda d: d["hosts"][0]["entry_roots"][0].update(path="cache"),
        # the filesystem root
        lambda d: d["hosts"][0]["entry_roots"][0].update(path="/"),
        # a root that is not normalized
        lambda d: d["hosts"][0]["entry_roots"][0].update(path="/var/cache/../x"),
        # a root for a command the host does not list as an entry command
        lambda d: d["hosts"][1].update(
            entry_roots=[{"command": "evict_cache", "path": "/var/cache/x"}]
        ),
        # an entry command run behind a transport prefix, where the local checks would not hold
        lambda d: d["hosts"][0].update(transport_prefix=["ssh", "pool-a"]),
        # an entry command carrying an argv of its own
        lambda d: d["commands"][0].update(argv=["rm", "-rf"]),
        # a worktree command with no lease marker
        lambda d: d["commands"][1].update(lease_marker_names=[]),
        # a command with no timeout
        lambda d: d["commands"][0].pop("timeout_seconds"),
        # an argv command with no argv
        lambda d: d["commands"][3].update(argv=[]),
    ],
)
def test_an_overlay_that_leaves_a_root_or_a_field_to_a_default_is_refused(
    tmp_path: Path, change: Any
) -> None:
    dirs = make_dirs(tmp_path)
    with pytest.raises(ValidationError):
        ModelHostStorageOverlay.model_validate(_mutated(dirs, change))


def test_no_source_of_the_node_holds_a_path_or_host_literal() -> None:
    offenders: list[str] = []
    for path in sorted(NODE_DIR.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and len(node.value) > 1
                and node.value.startswith(("/", "~", "$HOME"))
            ):
                offenders.append(f"{path.name}:{node.lineno}: {node.value!r}")
    assert offenders == []
