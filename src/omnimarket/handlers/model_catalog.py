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
import contextvars
import importlib
import json
import os
import stat
import tempfile
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from functools import cache
from pathlib import Path
from types import ModuleType
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


def _posix_locks() -> ModuleType | None:
    """The POSIX file-lock module, or None where there is none (Windows)."""
    try:
        return importlib.import_module("fcntl")
    except ImportError:
        return None


fcntl = _posix_locks()

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


def kept_models_dev_index(
    onex_home: Path | None = None, *, now: datetime | None = None
) -> ModelsDevIndex:
    """The models.dev copy as kept, without any fetch (OMN-23413 AC34).

    Reads the cached copy only while it is younger than its configured age.
    A missing, expired, or unreadable copy yields the empty index; this
    function never contacts models.dev.
    """
    settings = catalog_config()["models_dev"]
    path = (onex_home or _onex_home()) / str(settings["cache_file"])
    moment = now or datetime.now(UTC)
    cached_at: datetime | None = None
    if path.is_file():
        cached_at = datetime.fromtimestamp(path.stat().st_mtime, UTC)
    fresh = cached_at is not None and moment - cached_at < timedelta(
        hours=float(settings["max_age_hours"])
    )
    if not fresh:
        return EMPTY_INDEX
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return EMPTY_INDEX
    return ModelsDevIndex(
        models=_index(document),
        fetched_at=cached_at.isoformat() if cached_at else None,
        stale=False,
        error=None,
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

    It is data in the install (omnimarket#6497). A delegation call does not
    price from it: OMN-23413 AC35 prices from models.dev read live.
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


def call_read_deadline(remaining_seconds: float) -> float:
    """The total time a call may spend reading models.dev (OMN-23413 AC35).

    A share of the call's remaining time, never more than the configured cap,
    so optional price metadata cannot spend the provider call's own budget.
    """
    settings = catalog_config()["models_dev"]
    share = float(settings["call_read_budget_share"]) * max(remaining_seconds, 0.0)
    return max(0.0, min(float(settings["call_read_deadline_seconds"]), share))


def _failure_marker(onex_home: Path) -> Path:
    cache_file = str(catalog_config()["models_dev"]["cache_file"])
    return (onex_home / cache_file).with_suffix(".failed")


def _read_lock_path(onex_home: Path) -> Path:
    cache_file = str(catalog_config()["models_dev"]["cache_file"])
    return (onex_home / cache_file).with_suffix(".lock")


def _failed_recently(onex_home: Path, *, now: datetime | None = None) -> bool:
    """A call's models.dev read failed or ran out of time within the retry interval."""
    try:
        failed_at = datetime.fromtimestamp(
            _failure_marker(onex_home).stat().st_mtime, UTC
        )
    except OSError:
        return False
    interval = timedelta(
        seconds=float(
            catalog_config()["models_dev"]["call_retry_after_failure_seconds"]
        )
    )
    return (now or datetime.now(UTC)) - failed_at < interval


def _mark_failed(onex_home: Path) -> None:
    # Last writer wins on the marker (round-2 note N2): a caller that gives up
    # marks outside the read lock, so it can land just after a read that
    # succeeded cleared the marker. That is harmless, because a fresh kept
    # copy is always checked before the marker and wins.
    marker = _failure_marker(onex_home)
    with contextlib.suppress(OSError):
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
        os.utime(marker)


def _clear_failed(onex_home: Path) -> None:
    with contextlib.suppress(OSError):
        _failure_marker(onex_home).unlink(missing_ok=True)


@dataclass
class _ReadState:
    fetching: bool = False


#: Set inside a deadline-bounded read: whether this call's own read began.
_models_dev_read_state: contextvars.ContextVar[_ReadState | None] = (
    contextvars.ContextVar("_models_dev_read_state", default=None)
)


def pricing_models_dev_index() -> ModelsDevIndex:
    """The prices a delegation call reads to price a model (OMN-23413 AC35).

    models.dev read live: the kept copy while it is younger than its configured
    age, else read again. A failed read leaves a marker, and no call reads
    again until the retry interval has passed; until then the call is unpriced
    (an expired copy never prices a call, see ``_pricing_index``). At most one
    read is in flight per ONEX home, across threads and processes: a caller
    that finds a read in flight waits on its lock (bounded by its own
    deadline in ``_pricing_index``) and then uses what that read kept.
    """
    home = _onex_home()
    kept = kept_models_dev_index(home)
    if kept.fetched_at is not None:
        return kept
    if _failed_recently(home):
        return ModelsDevIndex(
            models={},
            fetched_at=None,
            stale=False,
            error="models.dev read failed recently; not read again yet",
        )

    lock_path = _read_lock_path(home)
    with contextlib.suppress(OSError):
        lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)
    except OSError:
        os.close(fd)
        raise

    try:
        # Another caller may have just finished while we waited on the lock.
        kept = kept_models_dev_index(home)
        if kept.fetched_at is not None:
            return kept
        if _failed_recently(home):
            return ModelsDevIndex(
                models={},
                fetched_at=None,
                stale=False,
                error="models.dev read failed recently; not read again yet",
            )

        state = _models_dev_read_state.get()
        if state is not None:
            state.fetching = True

        index = load_models_dev(home)
        if index.error is not None:
            _mark_failed(home)
        else:
            _clear_failed(home)
        return index
    finally:
        if fcntl is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _kept_pricing_index() -> ModelsDevIndex:
    """The fresh kept models.dev copy, read from disk only, or the empty index."""
    try:
        return kept_models_dev_index(_onex_home())
    except Exception:  # Any failure reading optional metadata: price unknown.
        return EMPTY_INDEX


def _pricing_index(deadline_seconds: float | None = None) -> ModelsDevIndex:
    """The index a call may price from, or the empty index.

    Supplemental metadata cannot prevent a provider call, so a read failure
    is no index and the model stays unpriced. An expired copy (a failed
    refetch that kept a stale cache) never prices a call. With a deadline, a
    fresh kept copy prices the call first with no network read, whatever the
    deadline, so a call is left unknown only when no fresh copy exists. A
    deadline of zero or less then starts no read. Otherwise the read runs on
    a daemon thread and is given up when the deadline passes. A failure is
    marked for the retry interval only when this call's own read began and
    ran out of the full configured deadline; a caller that only waited on
    another call's read, or gave up sooner, marks nothing. The thread may
    still finish on its own and keep, or mark, what it read.
    """
    if deadline_seconds is None:
        return _read_pricing_index()
    kept = _kept_pricing_index()
    if kept.fetched_at is not None:
        return kept
    if deadline_seconds <= 0:
        return EMPTY_INDEX
    state = _ReadState()
    found: list[ModelsDevIndex] = []

    def _run() -> None:
        _models_dev_read_state.set(state)
        found.append(_read_pricing_index())

    reader = threading.Thread(target=_run, name="models-dev-price-read", daemon=True)
    started = time.monotonic()
    reader.start()
    reader.join(max(deadline_seconds - (time.monotonic() - started), 0.0))
    if reader.is_alive() or not found:
        # A read this call began that ran out of the full configured deadline
        # counts as failed for the retry interval; a shorter wait says nothing
        # about models.dev, so it marks nothing and the read goes on to mark
        # its own outcome.
        cap = float(catalog_config()["models_dev"]["call_read_deadline_seconds"])
        if state.fetching and deadline_seconds >= cap:
            _mark_failed(_onex_home())
        return EMPTY_INDEX
    return found[0]


def _read_pricing_index() -> ModelsDevIndex:
    try:
        index = pricing_models_dev_index()
    except Exception:  # Any failure reading optional metadata: price unknown.
        return EMPTY_INDEX
    if index.stale:
        return EMPTY_INDEX
    return index


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


def _recalled_listing(
    provider: str,
    onex_home: Path | None = None,
    *,
    now: datetime | None = None,
) -> tuple[ModelByokModelListing, datetime] | None:
    """The kept list for ``provider`` while younger than its age, with its kept time.

    Returns the listing and the UTC time it was kept (file mtime). A missing,
    expired, or unreadable copy yields None.
    """
    path = _listing_path(provider, onex_home or _onex_home())
    settings = catalog_config()["provider_listing"]
    max_bytes = int(settings["max_bytes"])
    # Nonblocking, regular files only: the TUI reads this on its event loop.
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes:
                return None
            kept_at = datetime.fromtimestamp(info.st_mtime, UTC)
            max_age = timedelta(hours=float(settings["max_age_hours"]))
            if (now or datetime.now(UTC)) - kept_at >= max_age:
                return None
            raw = source.read(max_bytes + 1)
        if len(raw) > max_bytes:
            return None
        listing = ModelByokModelListing.model_validate_json(raw.decode("utf-8"))
    except (OSError, ValueError):
        return None
    return listing, kept_at


def provider_listing_expired(
    provider: str, onex_home: Path | None = None, *, now: datetime | None = None
) -> bool:
    """True when a kept list for ``provider`` exists and is older than its age.

    No kept list, or one that is not a regular file, is not an expired list.
    """
    path = _listing_path(provider, onex_home or _onex_home())
    try:
        info = os.stat(path)
    except OSError:
        return False
    if not stat.S_ISREG(info.st_mode):
        return False
    max_age = timedelta(
        hours=float(catalog_config()["provider_listing"]["max_age_hours"])
    )
    kept_at = datetime.fromtimestamp(info.st_mtime, UTC)
    return (now or datetime.now(UTC)) - kept_at >= max_age


def recalled_provider_listing(
    provider: str, onex_home: Path | None = None, *, now: datetime | None = None
) -> ModelByokModelListing | None:
    """The kept list for ``provider`` while younger than its age, else None."""
    result = _recalled_listing(provider, onex_home, now=now)
    if result is None:
        return None
    return result[0]


def list_and_remember_models(
    provider: str, api_key: str | SecretStr
) -> ModelByokModelListing:
    """Read ``provider``'s model list for the table and keep it for later calls."""
    listing = list_byok_models(provider, api_key)
    # A list that cannot be kept is still shown; calls fall back to models.dev.
    with contextlib.suppress(OSError):
        remember_provider_listing(listing)
    return listing


def models_dev_price_with_read_time(
    provider: str, model_id: str, *, deadline_seconds: float | None = None
) -> tuple[tuple[Decimal, Decimal], str | None] | None:
    """A model's models.dev ``(input, output)`` USD per 1M and when it was read, or None.

    OMN-23413 AC35: the provider's own catalogue and the model registry come
    first; this is read only for a model neither prices, from models.dev read
    live (``pricing_models_dev_index``). The read time is the ``fetched_at`` of
    the same index the price came from. ``provider`` is the ONEX provider id,
    mapped to models.dev's through ``provider_ids``. None when the provider has
    no models.dev id, models.dev does not list the model, or the entry states
    no input or output price.
    """
    md_provider = catalog_config()["provider_ids"].get(provider)
    if not md_provider:
        return None
    index = _pricing_index(deadline_seconds)
    metadata = index.models.get((str(md_provider), model_id))
    cost = metadata.get("cost") if metadata else None
    if not isinstance(cost, dict):
        return None
    price_in, price_out = _decimal(cost.get("input")), _decimal(cost.get("output"))
    if price_in is None or price_out is None:
        return None
    return (price_in, price_out), index.fetched_at


def models_dev_price_per_1m(
    provider: str, model_id: str
) -> tuple[Decimal, Decimal] | None:
    """A model's ``(input, output)`` USD per 1M tokens as models.dev lists it, or None."""
    found = models_dev_price_with_read_time(provider, model_id)
    return found[0] if found is not None else None


def _positive(value: Any) -> int | None:
    return value if type(value) is int and value > 0 else None


def _capabilities(
    metadata: Mapping[str, Any] | None, deployment: Deployment
) -> dict[str, CapabilityState]:
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
            elif "cost" in spec and deployment == "cloud":
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
    cost = metadata.get("cost") if metadata and deployment == "cloud" else None
    cost = cost if isinstance(cost, dict) else {}
    sources: dict[str, str] = {"model": provider_label}

    def prefer[T](field: str, stated: T | None, supplemental: T | None) -> T | None:
        if stated is not None:
            sources[field] = provider_label
            return stated
        if supplemental is not None:
            sources[field] = "models.dev"
        return supplemental

    context = prefer(
        "context", _positive(context_tokens), _positive(limit.get("context"))
    )
    max_output = prefer(
        "max_output", _positive(max_output_tokens), _positive(limit.get("output"))
    )
    catalog_name = metadata.get("name") if metadata else None
    display_name = prefer(
        "name", name or None, str(catalog_name) if catalog_name else None
    )
    price_in = prefer(
        "input", _decimal(input_usd_per_million), _decimal(cost.get("input"))
    )
    price_out = prefer(
        "output", _decimal(output_usd_per_million), _decimal(cost.get("output"))
    )
    price_sources = {sources[k] for k in ("input", "output") if k in sources}
    if price_sources:
        sources["price"] = (
            f"{provider_label} + models.dev"
            if len(price_sources) > 1
            else next(iter(price_sources))
        )
    cache_read = prefer("cache_read", None, _decimal(cost.get("cache_read")))
    cache_write = prefer("cache_write", None, _decimal(cost.get("cache_write")))
    tiered = bool(cost.get("tiers") or cost.get("context_over_200k"))
    if tiered:
        sources["pricing"] = "models.dev"
    merged = _capabilities(metadata, deployment)
    if metadata is not None:
        sources["capabilities"] = "models.dev"
        sources.update(
            {
                f"capability.{label}": "models.dev"
                for label, state in merged.items()
                if state != "unknown"
            }
        )
    stated = {k: v for k, v in (capabilities or {}).items() if v != "unknown"}
    if stated:
        merged.update(stated)
        sources["capabilities"] = provider_label
        sources.update({f"capability.{label}": provider_label for label in stated})
    return ModelCatalogEntry(
        provider=provider,
        provider_label=provider_label,
        model_id=model_id,
        server_url=server_url,
        display_name=display_name or model_id,
        deployment=deployment,
        context_tokens=context,
        max_output_tokens=max_output,
        input_usd_per_million=price_in,
        output_usd_per_million=price_out,
        cache_read_usd_per_million=cache_read,
        cache_write_usd_per_million=cache_write,
        tiered_price=tiered,
        capabilities=merged,
        status=status,
        status_reason=status_reason,
        sources=sources,
        metadata_fetched_at=index.fetched_at if metadata is not None else None,
        metadata_stale=index.stale and metadata is not None,
    )


def _provider_capabilities(model: ModelByokListedModel) -> dict[str, CapabilityState]:
    """What the provider's own row lists. Not listing a capability is not a "no"."""
    stated: dict[str, CapabilityState] = {}
    for spec in catalog_config()["capabilities"]:
        if (
            spec.get("provider_parameter") in model.supported_parameters
            or spec.get("provider_input") in model.input_modalities
        ):
            stated[str(spec["label"])] = "yes"
    return stated


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
        capabilities=_provider_capabilities(model),
    )


def runtime_catalog_entry(
    provider: str,
    model_id: str,
    *,
    live: bool = True,
    deadline_seconds: float | None = None,
) -> ModelCatalogEntry:
    """One combined entry: the provider list the explorer kept, then models.dev (AC34).

    It never lists models itself, so a delegation call makes no provider request
    beyond the call (OMN-20157). ``live`` reads models.dev as a call prices from
    it (AC35: read again when older than the configured age); ``live=False`` is
    for route resolution outside the call, which reads only a copy still within
    that age and never fetches. ``deadline_seconds`` bounds the live read.
    """
    index = _pricing_index(deadline_seconds) if live else kept_models_dev_index()
    result = _recalled_listing(provider)
    if result is not None:
        listing, kept_at = result
        for model in listing.models:
            if model.id == model_id:
                return listed_catalog_entry(
                    provider, model, index, provider_label=provider
                ).model_copy(update={"provider_listed_at": kept_at.isoformat()})
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
        deployment = deployment_of(provider)
        provider_models: dict[str, ModelByokListedModel] = {}
        expired = False
        if (
            deployment == "cloud"
            and not on_servers
            and provider in catalog_config()["provider_ids"]
        ):
            listing = recalled_provider_listing(provider)
            if listing and listing.provider == provider and listing.outcome == "listed":
                provider_models = {model.id: model for model in listing.models}
            elif listing is None:
                # AC4: models.dev fills the facts, and the table says why.
                expired = provider_listing_expired(provider)
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
            if model_id in provider_models:
                entry = listed_catalog_entry(
                    provider,
                    provider_models[model_id],
                    index,
                    provider_label=label_of(provider, None),
                )
                entries.append(
                    entry.model_copy(update={"status": status, "status_reason": reason})
                )
                continue
            said = (servers or {}).get((provider, model_id, url or ""))
            entries.append(
                catalog_entry(
                    provider,
                    model_id,
                    deployment=deployment,
                    index=index,
                    provider_label=label_of(provider, said.server if said else None),
                    context_tokens=said.context_tokens if said else None,
                    capabilities=said.capabilities if said else None,
                    status=status,
                    status_reason=reason,
                    server_url=url,
                ).model_copy(update={"provider_list_expired": expired})
            )
    return tuple(entries)
