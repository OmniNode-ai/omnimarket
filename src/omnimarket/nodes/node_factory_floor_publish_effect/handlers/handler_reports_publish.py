# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Publish the reports site beside the factory floor: an index of every report, each report's page and data.

The contract's ``runtime.reports`` block lists the reports. For each one this reads the data files that a node
or a lane wrote under the registry home, takes the data's own time (a top-level JSON key, else the file's
modified time) and copies the data and the packaged page to the directory the static server on the lab serves,
over ssh, each file written to a temporary name and renamed into place. A data file that is missing or
unreadable is not published and is listed as unread with its reason, so the index never shows a missing source as
a quiet one. The manifest goes last, so a reader that finds it finds every file it lists that could be read.
The serving host, its directory and its URL are the deployment overlay's, shared with the factory floor publish.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.nodes.node_factory_floor_publish_effect.handlers.handler_factory_floor_publish import (
    NODE_DIR,
    NODE_NAME,
    PublishRefusedError,
    Runner,
    copy_to_server,
    load_runtime,
    run_process,
)
from omnimarket.nodes.node_factory_floor_publish_effect.models.model_factory_floor_publish import (
    ModelFactoryFloorPublishOverlay,
    ModelReportData,
    ModelReportEntry,
    ModelReportsPublishRequest,
    ModelReportsPublishResult,
)

MANIFEST_SCHEMA = "omni.reports-site/1"
MAX_DATA_BYTES = 8 * 1024 * 1024


def _parse_time(value: object) -> datetime | None:
    """A timestamp or a bare date from a data file's own key, as an aware UTC time; ``None`` when it is neither."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return (
        parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    )


def _isoz(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class HandlerReportsPublish:
    """Boundary of the reports publish: the data reads and the copies happen here."""

    def __init__(self, runner: Runner = run_process) -> None:
        self._run = runner

    def handle(
        self,
        request: ModelReportsPublishRequest,
        environ: Mapping[str, str] | None = None,
    ) -> ModelReportsPublishResult:
        """Raises ``PublishRefusedError`` or ``NodeOverlayError`` naming what is missing."""
        env = os.environ if environ is None else environ
        rt = load_runtime()
        overlay = load_structured_overlay(
            NODE_NAME,
            ModelFactoryFloorPublishOverlay,
            env,
            explicit=request.overlay,
            file_env=rt.overlay_file_env,
        )
        home_value = (request.home or env.get(rt.home_env, "")).strip()
        if not home_value:
            raise PublishRefusedError(
                f"{rt.home_env} is not set (fail fast, no default): export it or pass --omni-home"
            )
        home = Path(home_value)
        now = request.now or datetime.now(UTC)
        reports = rt.reports

        files: list[tuple[str, bytes]] = []
        queued: set[str] = set()
        unread: list[str] = []
        manifest_entries: list[dict[str, object]] = []

        def queue(remote: str, payload: bytes) -> None:
            if remote in queued:
                raise PublishRefusedError(
                    f"two reports publish {remote}; each published name is written by one report"
                )
            queued.add(remote)
            files.append((remote, payload))

        for entry in reports.entries:
            data_rows: list[dict[str, object]] = []
            for item in entry.data:
                row, payload = self._read_data(home, item)
                data_rows.append(row)
                if payload is None:
                    unread.append(f"{entry.id}:{item.remote}")
                elif item.publish:
                    queue(item.remote, payload)
            for page in entry.pages:
                queue(page.remote, self._read_page(page.page_relpath))
            manifest_entries.append(self._manifest_entry(entry, data_rows))

        queue(reports.index.remote, self._read_page(reports.index.page_relpath))
        manifest = json.dumps(
            {
                "schema": MANIFEST_SCHEMA,
                "published_at": _isoz(now),
                "reports": manifest_entries,
            },
            sort_keys=True,
            indent=1,
        ).encode("utf-8")
        files.append((reports.manifest, manifest))

        for name, payload in files:
            copy_to_server(self._run, rt, overlay, name, payload)
        return ModelReportsPublishResult(
            published=tuple(name for name, _ in files),
            unread=tuple(unread),
            manifest_sha256=hashlib.sha256(manifest).hexdigest(),
            index_url=overlay.serve_url.rstrip("/") + "/" + reports.index.remote,
        )

    @staticmethod
    def _read_page(relpath: str) -> bytes:
        path = NODE_DIR / relpath
        try:
            return path.read_bytes()
        except OSError as exc:
            raise PublishRefusedError(
                f"the packaged page {relpath} is unreadable ({type(exc).__name__})"
            ) from exc

    @staticmethod
    def _read_data(
        home: Path, item: ModelReportData
    ) -> tuple[dict[str, object], bytes | None]:
        """The manifest row of one data file and its bytes; the bytes are ``None`` when the file is unread."""
        row: dict[str, object] = {
            "name": item.name,
            "remote": item.remote,
            "status": "unread",
            "as_of": None,
            "as_of_basis": None,
            "reason": "",
        }
        path = home / item.relpath
        try:
            stat = path.stat()
            if stat.st_size > MAX_DATA_BYTES:
                row["reason"] = f"larger than {MAX_DATA_BYTES} bytes"
                return row, None
            raw = path.read_bytes()
        except OSError as exc:
            row["reason"] = f"not readable ({type(exc).__name__})"
            return row, None
        if item.remote.endswith(".json") or item.as_of_key:
            try:
                document = json.loads(raw)
            except ValueError:
                row["reason"] = "not JSON"
                return row, None
            if item.as_of_key:
                moment = _parse_time(
                    document.get(item.as_of_key) if isinstance(document, dict) else None
                )
                if moment is None:
                    row["reason"] = f"no readable time at key {item.as_of_key}"
                    return row, None
                row["as_of"] = _isoz(moment)
                row["as_of_basis"] = f"the file's own {item.as_of_key} value"
        else:
            try:
                raw.decode("utf-8")
            except UnicodeDecodeError:
                row["reason"] = "not UTF-8 text"
                return row, None
        if row["as_of"] is None:
            row["as_of"] = _isoz(datetime.fromtimestamp(stat.st_mtime, UTC))
            row["as_of_basis"] = "the file's modified time"
        row["status"] = "read"
        return row, raw

    @staticmethod
    def _manifest_entry(
        entry: ModelReportEntry, data_rows: list[dict[str, object]]
    ) -> dict[str, object]:
        return {
            "id": entry.id,
            "title": entry.title,
            "summary": entry.summary,
            "href": entry.href,
            "refresh": entry.refresh,
            "refresh_note": entry.refresh_note,
            "data": data_rows,
        }


__all__ = ["MANIFEST_SCHEMA", "HandlerReportsPublish"]
