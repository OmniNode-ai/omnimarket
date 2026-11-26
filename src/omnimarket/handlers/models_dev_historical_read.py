# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""models.dev as a stored run is priced from (OMN-23413 AC35).

The projection read behind the local dashboard and ``onex metering`` prices a
stored zero from models.dev read live: the cache under the ONEX home while it
is younger than its configured age, else read again. That read must not stall
a served read, so it waits at most ``deadline_seconds`` in total, and after a
failed read it does not try again for ``retry_after_seconds`` (a marker file
under the ONEX home, so a later process honours it too). Until then the runs it
would price stay unknown. A fresh copy that cannot be parsed is read again
rather than kept until it ages out. ``model_catalog.load_models_dev`` itself is
unchanged; the deadline and the marker belong to this read path only.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from omnimarket.handlers import model_catalog
from omnimarket.handlers.model_catalog import (
    EMPTY_INDEX,
    FetchBytes,
    ModelsDevIndex,
)

__all__ = ["historical_models_dev_index", "models_dev_declares_free"]


def historical_models_dev_index() -> ModelsDevIndex:
    """models.dev for pricing a stored run, or the empty index.

    A copy older than its configured age (a failed refetch keeps it, marked
    stale) is never used, and neither is the price list shipped with the
    install. Tests replace this function unless they serve their own copy.
    """
    try:
        # The ONEX home the model table and the delegation call share.
        return _read(model_catalog.onex_home())
    except Exception:  # Any failure reading optional metadata: price unknown.
        return EMPTY_INDEX


def models_dev_declares_free(
    index: ModelsDevIndex, provider: str, model_id: str
) -> bool:
    """True when ``index`` lists ``model_id`` at a zero input and output price.

    ``provider`` is the ONEX provider id, mapped to models.dev's through the
    catalogue's ``provider_ids``. A model models.dev does not list, or whose
    entry states no input or output price, is not free.
    """
    md_provider = model_catalog.models_dev_provider_id(provider)
    if not md_provider:
        return False
    metadata = index.models.get((str(md_provider), model_id))
    cost = metadata.get("cost") if metadata else None
    if not isinstance(cost, Mapping):
        return False
    return _zero(cost.get("input")) and _zero(cost.get("output"))


def _zero(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        return False
    try:
        return Decimal(str(value)) == 0
    except InvalidOperation:
        return False


def _settings() -> Mapping[str, Any]:
    settings: Mapping[str, Any] = model_catalog.catalog_config()[
        "historical_price_read"
    ]
    return settings


def _read(onex_home: Path) -> ModelsDevIndex:
    settings = _settings()
    marker = onex_home / str(settings["failed_marker_file"])
    may_fetch = not _failed_recently(marker, float(settings["retry_after_seconds"]))
    fetch = (
        _deadline_fetch(float(settings["deadline_seconds"])) if may_fetch else _no_fetch
    )
    index = model_catalog.load_models_dev(onex_home, fetch=fetch)
    if may_fetch and index.stale and index.fetched_at is None and index.error is None:
        # A copy younger than its age that cannot be parsed: read models.dev
        # again now, by reading as if the copy had aged out.
        max_age = float(model_catalog.catalog_config()["models_dev"]["max_age_hours"])
        forced = model_catalog.load_models_dev(
            onex_home,
            fetch=fetch,
            now=datetime.now(UTC) + timedelta(hours=max_age),
        )
        index = (
            forced
            if forced.error is not None
            else model_catalog.load_models_dev(onex_home, fetch=_no_fetch)
        )
    if may_fetch and index.error is not None:
        _mark_failed(marker)
    if index.fetched_at is None or index.stale:
        return EMPTY_INDEX
    return index


def _failed_recently(marker: Path, retry_after_seconds: float) -> bool:
    try:
        failed_at = marker.stat().st_mtime
    except OSError:
        return False
    return time.time() - failed_at < retry_after_seconds


def _mark_failed(marker: Path) -> None:
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
    except OSError:
        # Accepted, not fixed: with an ONEX home the marker cannot be written in,
        # every read tries models.dev again, each bounded by deadline_seconds. A
        # served read runs two queries, so until the home is writable each
        # refresh can wait about two deadlines (about 4 s at the configured 2 s).
        return


def _no_fetch(url: str, timeout: float) -> bytes:
    raise TimeoutError("models.dev failed recently; not read again yet")


def _deadline_fetch(deadline_seconds: float) -> FetchBytes:
    """``model_catalog.fetch_models_dev`` bounded by ``deadline_seconds`` in total.

    The fetch runs in a daemon thread; one still running at the deadline is
    abandoned and its answer, if any, is discarded.
    """

    def fetch(url: str, timeout: float) -> bytes:
        outcome: dict[str, bytes | Exception] = {}

        def run() -> None:
            try:
                # models.dev through the catalogue's own contract transport.
                outcome["body"] = model_catalog.fetch_models_dev(
                    url, min(timeout, deadline_seconds)
                )
            except Exception as exc:  # handed to the caller below
                outcome["error"] = exc

        worker = threading.Thread(
            target=run, name="models-dev-historical-read", daemon=True
        )
        worker.start()
        worker.join(deadline_seconds)
        body = outcome.get("body")
        if isinstance(body, bytes):
            return body
        error = outcome.get("error")
        if isinstance(error, Exception):
            raise error
        raise TimeoutError(f"models.dev did not answer within {deadline_seconds}s")

    return fetch
