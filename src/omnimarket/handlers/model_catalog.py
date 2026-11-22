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

import contextlib
import json
import os
import tempfile
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from functools import cache
from pathlib import Path
from typing import Any, NamedTuple

import yaml
from pydantic import SecretStr

from omnimarket.config.service_endpoints import service_endpoint_url
from omnimarket.handlers.routing.byok_model_listing import (
    ModelByokListedModel,
    ModelByokModelListing,
    list_byok_models,
)
from omnimarket.models.model_catalog_entry import (
    CapabilityState,
    Deployment,
    ModelCatalogEntry,
    ModelStatus,
)
from omnimarket.models.model_local_server_details import ModelLocalServerDetails

_CONFIG_PATH = Path(__file__).resolve().parents[1] / "configs" / "model_catalog.yaml"
_PRICE_LIST_DIR = Path(__file__).resolve().parents[1] / "data" / "model_prices"

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
    """models.dev through the delegation effect's contract transport."""
    from omnimarket.nodes.node_llm_delegation_call_effect.handlers import transport

    document = transport.get_provider_json(
        url=url,
        timeout_seconds=timeout,
        max_bytes=int(catalog_config()["models_dev"]["max_bytes"]),
        follow_redirects=True,
    )
    return json.dumps(document).encode("utf-8")


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


def _onex_home() -> Path:
    """The ONEX home folder the model table and the delegation call share."""
    from omnibase_core.cli.cli_user_config import user_config_path

    return user_config_path().parent


def onex_home() -> Path:
    """The ONEX home folder; looks up ``_onex_home`` at call time."""
    return _onex_home()


def fetch_models_dev(url: str, timeout: float) -> bytes:
    """models.dev bytes; looks up ``_fetch`` at call time."""
    return _fetch(url, timeout)


@cache
def _shipped_price_list() -> Mapping[str, Any]:
    """The price list that ships inside the install (``model_price_list.v1.json``)."""
    name = str(catalog_config()["shipped_price_list"]["file"])
    document = json.loads((_PRICE_LIST_DIR / name).read_text(encoding="utf-8"))
    if not isinstance(document, dict) or not isinstance(
        document.get("providers"), dict
    ):
        raise ValueError(f"{name} must hold a providers mapping")
    return document


def shipped_pricing_index() -> ModelsDevIndex:
    """The shipped price list as an index of ``cost`` entries, read from disk only.

    It is data in the install, refreshed with each release, so a delegation call
    on a customer machine never reaches models.dev to price a model.
    """
    document = _shipped_price_list()
    found: dict[tuple[str, str], Mapping[str, Any]] = {}
    for provider_id, models in document["providers"].items():
        if not isinstance(models, dict):
            continue
        for model_id, price in models.items():
            if isinstance(price, list) and len(price) == 2:
                found[(str(provider_id), str(model_id))] = {
                    "cost": {"input": price[0], "output": price[1]}
                }
    return ModelsDevIndex(
        models=found,
        fetched_at=str(document.get("read_on")),
        stale=False,
        error=None,
    )


def pricing_models_dev_index() -> ModelsDevIndex:
    """The prices a delegation call reads to price a model (OMN-23413 AC35).

    The price list shipped with the install, never a fetch: a customer machine
    makes no models.dev connection inside a delegation call. The model table's
    own refresh (``load_models_dev``) is a separate, user-initiated read.
    Tests replace this function so no unit test depends on the list's contents.
    """
    return shipped_pricing_index()


def _pricing_index() -> ModelsDevIndex:
    """The index a call may price from, or the empty index.

    Supplemental metadata cannot prevent a provider call, so a read failure is
    no index and the model stays unpriced.
    """
    try:
        return pricing_models_dev_index()
    except Exception:  # Any failure reading optional metadata: price unknown.
        return EMPTY_INDEX


def _listing_path(provider: str, onex_home: Path) -> Path:
    pattern = str(catalog_config()["provider_listing"]["cache_file"])
    return onex_home / pattern.format(provider=provider)


def remember_provider_listing(
    listing: ModelByokModelListing, onex_home: Path | None = None
) -> None:
    """Keep a provider list that was read, for later calls to price from (AC34).

    Only a list that was actually read is kept; a refused or failed read leaves
    the earlier copy as it was.
    """
    if listing.outcome != "listed":
        return
    path = _listing_path(listing.provider, onex_home or _onex_home())
    _write_atomic(path, listing.model_dump_json().encode("utf-8"))


def recalled_provider_listing(
    provider: str, onex_home: Path | None = None, *, now: datetime | None = None
) -> ModelByokModelListing | None:
    """The kept list for ``provider`` while younger than its age, else None."""
    path = _listing_path(provider, onex_home or _onex_home())
    try:
        kept_at = datetime.fromtimestamp(path.stat().st_mtime, UTC)
    except OSError:
        return None
    max_age = timedelta(
        hours=float(catalog_config()["provider_listing"]["max_age_hours"])
    )
    if (now or datetime.now(UTC)) - kept_at >= max_age:
        return None
    try:
        return ModelByokModelListing.model_validate_json(path.read_text("utf-8"))
    except (OSError, ValueError):
        return None


def list_and_remember_models(
    provider: str, api_key: str | SecretStr
) -> ModelByokModelListing:
    """Read ``provider``'s model list for the table and keep it for later calls."""
    listing = list_byok_models(provider, api_key)
    # A list that cannot be kept is still shown; calls fall back to models.dev.
    with contextlib.suppress(OSError):
        remember_provider_listing(listing)
    return listing


def models_dev_price_per_1m(
    provider: str, model_id: str
) -> tuple[Decimal, Decimal] | None:
    """A model's ``(input, output)`` USD per 1M tokens as models.dev lists it, or None.

    OMN-23413 AC35: the provider's own catalogue and the model registry come
    first; this is read only for a model neither prices, from the price list
    shipped with the install. ``provider`` is the ONEX
    provider id, mapped to models.dev's through ``provider_ids``. None when the
    provider has no models.dev id, models.dev does not list the model, or the
    entry states no input or output price.
    """
    md_provider = catalog_config()["provider_ids"].get(provider)
    if not md_provider:
        return None
    metadata = _pricing_index().models.get((str(md_provider), model_id))
    cost = metadata.get("cost") if metadata else None
    if not isinstance(cost, dict):
        return None
    price_in, price_out = _decimal(cost.get("input")), _decimal(cost.get("output"))
    if price_in is None or price_out is None:
        return None
    return price_in, price_out


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
    max_output_tokens: int | None = None,
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
    if deployment == "cloud":
        fallback_in = _decimal(cost.get("input")) if price_in is None else None
        fallback_out = _decimal(cost.get("output")) if price_out is None else None
        if fallback_in is not None or fallback_out is not None:
            sources["price"] = (
                f"{provider_label} + models.dev" if "price" in sources else "models.dev"
            )
        price_in = price_in if price_in is not None else fallback_in
        price_out = price_out if price_out is not None else fallback_out
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
        max_output_tokens=_positive(max_output_tokens)
        or _positive(limit.get("output")),
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


def listed_catalog_entry(
    provider: str,
    model: ModelByokListedModel,
    index: ModelsDevIndex,
    *,
    provider_label: str,
) -> ModelCatalogEntry:
    """Merge a provider-listed model once for both the table and the call."""
    return catalog_entry(
        provider,
        model.id,
        deployment="cloud",
        index=index,
        provider_label=provider_label,
        name=model.name if model.name != model.id else None,
        context_tokens=model.context_tokens,
        max_output_tokens=model.max_output_tokens,
        input_usd_per_million=model.input_usd_per_million,
        output_usd_per_million=model.output_usd_per_million,
    )


def runtime_catalog_entry(provider: str, model_id: str) -> ModelCatalogEntry:
    """Provider facts as the explorer last read them, and the same supplemental cache.

    Reads only what is kept: the provider list the table last read (AC34) and
    the cached models.dev index. It never lists models itself, so a delegation
    call makes no request beyond the call (OMN-20157).
    """
    index = _pricing_index()
    listing = recalled_provider_listing(provider)
    if listing is not None:
        for model in listing.models:
            if model.id == model_id:
                return listed_catalog_entry(
                    provider, model, index, provider_label=provider
                )
    return catalog_entry(
        provider,
        model_id,
        deployment="cloud",
        index=index,
        provider_label=provider,
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
