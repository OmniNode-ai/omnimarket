# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The model catalogue every onex screen shows (OMN-23413).

One merge, used by /config, /model, /delegation and `onex models`: the
provider's own list decides which models exist, their ids and the provider's
published context and price; models.dev fills only what the provider did not
state, matched exactly by provider and model id; real pinned tests decide
status. What no source states stays unknown. The rules (which models.dev
provider an ONEX provider is, which capabilities are shown and how each is
read, the cache) are data in configs/model_catalog.yaml.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from functools import cache
from pathlib import Path
from typing import Any, NamedTuple

import httpx
import yaml

from omnimarket.config.service_endpoints import service_endpoint_url
from omnimarket.models.model_catalog_entry import (
    CapabilityState,
    Deployment,
    ModelCatalogEntry,
    ModelStatus,
)
from omnimarket.models.model_local_server_details import ModelLocalServerDetails

_CONFIG_PATH = Path(__file__).resolve().parents[1] / "configs" / "model_catalog.yaml"

#: Reads a URL within a timeout and returns its body; raises on any failure.
FetchBytes = Callable[[str, float], bytes]


class ModelsDevIndex(NamedTuple):
    """models.dev, keyed by (models.dev provider id, model id)."""

    models: Mapping[tuple[str, str], Mapping[str, Any]]
    fetched_at: str | None
    stale: bool
    error: str | None


EMPTY_INDEX = ModelsDevIndex(models={}, fetched_at=None, stale=False, error=None)


@cache
def catalog_config() -> Mapping[str, Any]:
    document = yaml.safe_load(_CONFIG_PATH.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("model_catalog.yaml must be a mapping")
    return document


def _fetch(url: str, timeout: float) -> bytes:
    response = httpx.get(url, timeout=timeout, follow_redirects=True)
    response.raise_for_status()
    return response.content


def _index(document: Any) -> dict[tuple[str, str], Mapping[str, Any]]:
    wanted = set(catalog_config()["provider_ids"].values())
    found: dict[tuple[str, str], Mapping[str, Any]] = {}
    if not isinstance(document, dict):
        return found
    for provider_id, provider in document.items():
        if provider_id not in wanted or not isinstance(provider, dict):
            continue
        models = provider.get("models")
        if isinstance(models, dict):
            for model_id, model in models.items():
                if isinstance(model, dict):
                    found[(str(provider_id), str(model_id))] = model
    return found


def _write_atomic(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".models_dev_")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(body)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def load_models_dev(
    onex_home: Path,
    *,
    fetch: FetchBytes | None = None,
    now: datetime | None = None,
) -> ModelsDevIndex:
    """models.dev from the cache, refetched when older than its configured age.

    A failed refetch keeps the cache and marks it stale; with no cache at all
    the index is empty and every supplemental field stays unknown.
    """
    settings = catalog_config()["models_dev"]
    path = onex_home / str(settings["cache_file"])
    moment = now or datetime.now(UTC)
    cached_at: datetime | None = None
    if path.is_file():
        cached_at = datetime.fromtimestamp(path.stat().st_mtime, UTC)
    fresh = cached_at is not None and moment - cached_at < timedelta(
        hours=float(settings["max_age_hours"])
    )
    error: str | None = None
    if not fresh:
        try:
            body = (fetch or _fetch)(
                service_endpoint_url(str(settings["endpoint"])),
                float(settings["timeout_seconds"]),
            )
            json.loads(body)  # never cache a body that is not JSON
            _write_atomic(path, body)
            cached_at = moment
        except Exception as exc:  # any failure: keep what is cached, say so
            error = f"models.dev could not be read ({type(exc).__name__})"
    if not path.is_file():
        return ModelsDevIndex(models={}, fetched_at=None, stale=False, error=error)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return ModelsDevIndex(models={}, fetched_at=None, stale=True, error=error)
    return ModelsDevIndex(
        models=_index(document),
        fetched_at=cached_at.isoformat() if cached_at else None,
        stale=error is not None,
        error=error,
    )


def _decimal(value: Any) -> Decimal | None:
    """A non-negative number as Decimal through its text, or None."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    return number if number.is_finite() and number >= 0 else None


def _positive(value: Any) -> int | None:
    return value if isinstance(value, int) and value > 0 else None


def _capabilities(metadata: Mapping[str, Any] | None) -> dict[str, CapabilityState]:
    found: dict[str, CapabilityState] = {}
    for spec in catalog_config()["capabilities"]:
        label = str(spec["label"])
        state: CapabilityState = "unknown"
        if metadata is not None:
            if "field" in spec:
                value = metadata.get(str(spec["field"]))
                if isinstance(value, bool):
                    state = "yes" if value else "no"
            elif "input" in spec:
                modalities = metadata.get("modalities")
                inputs = (
                    modalities.get("input") if isinstance(modalities, dict) else None
                )
                if isinstance(inputs, list):
                    state = "yes" if str(spec["input"]) in inputs else "no"
            elif "cost" in spec:
                cost = metadata.get("cost")
                if (
                    isinstance(cost, dict)
                    and _decimal(cost.get(str(spec["cost"]))) is not None
                ):
                    state = "yes"
        found[label] = state
    return found


def catalog_entry(
    provider: str,
    model_id: str,
    *,
    deployment: Deployment,
    index: ModelsDevIndex,
    provider_label: str,
    name: str | None = None,
    context_tokens: int | None = None,
    input_usd_per_million: Any = None,
    output_usd_per_million: Any = None,
    status: ModelStatus = "listed",
    status_reason: str | None = None,
    capabilities: Mapping[str, CapabilityState] | None = None,
    server_url: str | None = None,
) -> ModelCatalogEntry:
    """One model: the provider's own values first, models.dev only for the rest.

    ``capabilities`` are what the provider or the user's own server stated;
    each one it states wins, and models.dev fills only the ones it left out.
    """
    md_provider = catalog_config()["provider_ids"].get(provider)
    metadata = index.models.get((md_provider, model_id)) if md_provider else None
    limit = metadata.get("limit") if metadata else None
    limit = limit if isinstance(limit, dict) else {}
    cost = metadata.get("cost") if metadata else None
    cost = cost if isinstance(cost, dict) else {}
    sources: dict[str, str] = {"model": provider_label}

    context = _positive(context_tokens)
    if context is not None:
        sources["context"] = provider_label
    elif (context := _positive(limit.get("context"))) is not None:
        sources["context"] = "models.dev"

    price_in = _decimal(input_usd_per_million)
    price_out = _decimal(output_usd_per_million)
    if price_in is not None or price_out is not None:
        sources["price"] = provider_label
    elif deployment == "cloud":
        price_in, price_out = _decimal(cost.get("input")), _decimal(cost.get("output"))
        if price_in is not None or price_out is not None:
            sources["price"] = "models.dev"
    merged = _capabilities(metadata)
    if metadata is not None:
        sources["capabilities"] = "models.dev"
    stated = {k: v for k, v in (capabilities or {}).items() if v != "unknown"}
    if stated:
        merged.update(stated)
        sources["capabilities"] = provider_label
    return ModelCatalogEntry(
        provider=provider,
        provider_label=provider_label,
        model_id=model_id,
        server_url=server_url,
        display_name=name
        or (
            str(metadata.get("name")) if metadata and metadata.get("name") else model_id
        ),
        deployment=deployment,
        context_tokens=context,
        max_output_tokens=_positive(limit.get("output")),
        input_usd_per_million=price_in,
        output_usd_per_million=price_out,
        cache_read_usd_per_million=_decimal(cost.get("cache_read")),
        cache_write_usd_per_million=_decimal(cost.get("cache_write")),
        tiered_price=bool(cost.get("tiers") or cost.get("context_over_200k")),
        capabilities=merged,
        status=status,
        status_reason=status_reason,
        sources=sources,
        metadata_fetched_at=index.fetched_at if metadata is not None else None,
        metadata_stale=index.stale and metadata is not None,
    )


def stored_status(
    model_id: str, last_test: Mapping[str, Any] | None
) -> tuple[ModelStatus, str | None]:
    """A stored model's status from its provider's last pinned test."""
    if not last_test or last_test.get("model") != model_id:
        return "untested", None
    if last_test.get("status") == "passed":
        return "ready", None
    return "failed", str(last_test.get("reason") or last_test.get("status") or "")


def stored_entries(
    rows: Iterable[Mapping[str, Any]],
    *,
    index: ModelsDevIndex,
    deployment_of: Callable[[str], Deployment],
    label_of: Callable[[str, str | None], str],
    servers: Mapping[tuple[str, str, str], ModelLocalServerDetails] | None = None,
) -> tuple[ModelCatalogEntry, ...]:
    """Every model the user selected, in their order, from `onex models list --json` rows.

    ``servers`` holds what each model's own server said, by (provider, model,
    server URL): it names who serves the model and states its context and
    capabilities. OMN-23413: a model added by URL is one entry per server (the
    row's ``local_models``), each with its own server's last test.
    """
    entries: list[ModelCatalogEntry] = []
    for row in rows:
        if not row.get("set_up"):
            continue
        provider = str(row.get("provider"))
        last = row.get("last_test")
        on_servers = row.get("local_models")
        stored: list[tuple[str, str | None, Any]] = (
            [
                (str(m["model"]), str(m["base_url"]), m.get("last_test"))
                for m in on_servers
                if isinstance(m, dict) and m.get("model") and m.get("base_url")
            ]
            if isinstance(on_servers, list) and on_servers
            else [(str(model), None, last) for model in row.get("models") or ()]
        )
        for model_id, url, tested in stored:
            status, reason = stored_status(
                model_id, tested if isinstance(tested, dict) else None
            )
            said = (servers or {}).get((provider, model_id, url or ""))
            entries.append(
                catalog_entry(
                    provider,
                    model_id,
                    deployment=deployment_of(provider),
                    index=index,
                    provider_label=label_of(provider, said.server if said else None),
                    context_tokens=said.context_tokens if said else None,
                    capabilities=said.capabilities if said else None,
                    status=status,
                    status_reason=reason,
                    server_url=url,
                )
            )
    return tuple(entries)
