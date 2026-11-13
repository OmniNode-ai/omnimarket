# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A host's last-known overlays: a directory with the layout of an overlay root, and a stamp per file.

``<root>/<overlay id>/overlay.yaml`` is what ``find_node_overlay`` and a table file's parsers read, so a
reader is cut over by naming this root, with no import of this package. A file holds one physical line
per scalar and the document's own key order, because the flat parsers depend on both. The stamp beside
it carries the digest, the file's hash and the time the source was last asserted. File and stamp are each
written by rename; the hash shows whether a reader caught a mismatched pair.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

import yaml
from pydantic import JsonValue

from omnimarket.handlers.node_overlay_reader import OVERLAY_FILE_NAME
from omnimarket.models.overlay_store import (
    ModelOverlaySnapshotStamp,
    ModelOverlayStoreRecord,
)

_NO_FOLD = 1 << 20


class _OverlayDumper(yaml.SafeDumper):
    """Double-quotes a string holding an apostrophe: the flat parsers strip quotes and undo no doubling."""


def _represent_str(dumper: yaml.SafeDumper, value: str) -> yaml.ScalarNode:
    style = '"' if "'" in value else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


_OverlayDumper.add_representer(str, _represent_str)


class OverlaySnapshotError(RuntimeError):
    """There is no snapshot, or the file does not match its stamp."""


def render_document(document: Mapping[str, JsonValue]) -> bytes:
    """The overlay file's bytes: deterministic, key order kept, no folded line."""
    return yaml.dump(
        dict(document),
        Dumper=_OverlayDumper,
        sort_keys=False,
        width=_NO_FOLD,
        default_flow_style=False,
        allow_unicode=True,
    ).encode("utf-8")


def _write_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(handle, "wb") as out:
            out.write(payload)
            out.flush()
            os.fsync(out.fileno())
        Path(temp).replace(path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


class FileOverlaySnapshot:
    """The snapshot files under ``root``."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def path_of(self, overlay_id: str) -> Path:
        return self._root / overlay_id / OVERLAY_FILE_NAME

    def _stamp_of(self, overlay_id: str) -> Path:
        path = self.path_of(overlay_id)
        return path.with_name(path.name + ".stamp.json")

    def write(
        self, loaded: ModelOverlayStoreRecord, *, written_at: datetime
    ) -> ModelOverlaySnapshotStamp:
        payload = render_document(loaded.document)
        stamp = ModelOverlaySnapshotStamp(
            overlay_id=loaded.overlay_id,
            digest=loaded.digest,
            file_sha256=hashlib.sha256(payload).hexdigest(),
            source_ref=loaded.source_ref,
            loaded_at=loaded.loaded_at,
            written_at=written_at,
        )
        _write_atomic(self.path_of(loaded.overlay_id), payload)
        _write_atomic(
            self._stamp_of(loaded.overlay_id),
            stamp.model_dump_json(indent=2).encode("utf-8"),
        )
        return stamp

    def read_stamp(self, overlay_id: str) -> ModelOverlaySnapshotStamp:
        """The stamp, once the file on disk is shown to be what it describes."""
        path = self.path_of(overlay_id)
        try:
            stamp = ModelOverlaySnapshotStamp.model_validate_json(
                self._stamp_of(overlay_id).read_bytes()
            )
            payload = path.read_bytes()
        except FileNotFoundError as exc:
            raise OverlaySnapshotError(
                f"no snapshot: {exc.filename} is absent"
            ) from None
        except (OSError, ValueError) as exc:
            raise OverlaySnapshotError(
                f"snapshot {path} is unreadable ({type(exc).__name__})"
            ) from None
        found = hashlib.sha256(payload).hexdigest()
        if found != stamp.file_sha256:
            raise OverlaySnapshotError(
                f"snapshot {path} hashes to {found[:12]}, its stamp says {stamp.file_sha256[:12]}"
            )
        return stamp

    def read_document(self, overlay_id: str) -> dict[str, JsonValue]:
        loaded = yaml.safe_load(self.path_of(overlay_id).read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise OverlaySnapshotError(f"snapshot of {overlay_id} is not a mapping")
        return loaded


__all__ = ["FileOverlaySnapshot", "OverlaySnapshotError", "render_document"]
