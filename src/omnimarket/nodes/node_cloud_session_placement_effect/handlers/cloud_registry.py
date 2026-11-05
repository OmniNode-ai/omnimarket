# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The cloud account registry: login identity, transport candidates, the login map and the one-time import.

Every function here is pure over its arguments. The caller runs ``claude auth status`` and the cloud probe (over its
own ssh seam) and hands the raw texts in; the node parses them and makes the sentences.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from omnimarket.models.cloud_session_placement import (
    ModelCloudAccount,
    ModelCloudLoginObservation,
    ModelCloudTransportRow,
)

STAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
CONSENT_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
HASH_RE = re.compile(r"[0-9a-f]{10}")
HASH_LEN = 10
REGISTRY_EMPTY = "the registry names no cloud account"


def stamp_of(moment: datetime) -> str:
    return f"{moment:{STAMP_FORMAT}}"


def parse_moment(text: str) -> datetime | None:
    """A full ``YYYY-MM-DDTHH:MM:SSZ`` stamp or a bare ``YYYY-MM-DD`` date as a UTC moment, else None."""
    for fmt in (STAMP_FORMAT, "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def recorded_hold(entry: Mapping[str, Any], hold_stamp: str) -> bool:
    """Keep deliberate/refusal holds, excluding the old rate-only inference."""
    return entry.get("limited_source") == hold_stamp and not re.fullmatch(
        r"[\w-]+ limit rejected", str(entry.get("limited_why") or "")
    )


def hash10(value: str) -> str:
    """The first 10 hex of the sha256 of ``value``: how an org id or an email is named in the registry."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:HASH_LEN]


def load_registry(accounts: Sequence[ModelCloudAccount]) -> str | None:
    """Why the registry cannot be used (the absent key, duplicate label or duplicate pair, by name), or None."""
    labels: set[str] = set()
    pairs: dict[tuple[str, str], str] = {}
    for position, a in enumerate(accounts):
        who = f"accounts[{position}]" + (f" {a.label!r}" if a.label else "")
        for key in ("label", "org_hash", "email_hash", "consent"):
            if not getattr(a, key):
                return f"{who} lacks {key}"
        for key in ("org_hash", "email_hash"):
            if not HASH_RE.fullmatch(getattr(a, key)):
                return f"{who} has a {key} that is not {HASH_LEN} lowercase hex"
        for key in ("consent_expires", "credit_expires"):
            value = getattr(a, key)
            if value and parse_moment(value) is None:
                return (
                    f"{who} has a {key} that is neither a stamp nor a date: {value!r}"
                )
        if a.label in labels:
            return f"duplicate account label {a.label!r}"
        pair = (a.org_hash, a.email_hash)
        if pair in pairs:
            return f"accounts {pairs[pair]!r} and {a.label!r} share one hash pair"
        labels.add(a.label)
        pairs[pair] = a.label
    return None


def parse_auth_status(text: str) -> tuple[str, str] | None:
    """(org hash, email hash) from ``claude auth status --json`` text, or None when the host is logged out or the
    text names no organization or email. Only hashes leave this function."""
    data: Any = None
    for candidate in [text, *text.splitlines()]:
        candidate = candidate.strip()
        if candidate.startswith("{"):
            try:
                data = json.loads(candidate)
            except ValueError:
                continue
            break
    if not isinstance(data, dict) or data.get("loggedIn") is False:
        return None
    org = str(data.get("orgId") or "").strip()
    email = str(data.get("email") or "").strip()
    if not org or not email:
        return None
    return hash10(org), hash10(email.lower())


def account_for(
    accounts: Sequence[ModelCloudAccount], org_hash: str, email_hash: str
) -> ModelCloudAccount | None:
    return next(
        (a for a in accounts if (a.org_hash, a.email_hash) == (org_hash, email_hash)),
        None,
    )


def new_state() -> dict[str, Any]:
    return {
        "version": 2,
        "accounts": {},
        "logins": {},
        "unconsented": {},
        "unreachable": {},
    }


def observe_logins(
    state: dict[str, Any],
    accounts: Sequence[ModelCloudAccount],
    observations: Sequence[ModelCloudLoginObservation],
    now: datetime,
    cloud_login: Callable[[str], str],
    login_ok: str,
) -> list[str]:
    """Replace each observed host's entry: the host leaves every account it was in, then enters the one its hash pair
    names (with the probe verdict) or the unconsented list. Returns one sentence per host that could not be read."""
    stamp = stamp_of(now)
    sentences: list[str] = []
    for ob in observations:
        for held in state["logins"].values():
            held.pop(ob.host, None)
        state["unconsented"].pop(ob.host, None)
        state["unreachable"].pop(ob.host, None)
        if not ob.reachable:
            state["unreachable"][ob.host] = {"seen_at": stamp}
            sentences.append(f"CLOUD-LOGIN host={ob.host} unreachable at {stamp}")
            continue
        pair = parse_auth_status(ob.auth_text)
        if pair is None:
            sentences.append(
                f"CLOUD-LOGIN host={ob.host} is logged out of claude.ai at {stamp}"
            )
            continue
        account = account_for(accounts, *pair)
        if account is None:
            state["unconsented"][ob.host] = {
                "org_hash": pair[0],
                "email_hash": pair[1],
                "seen_at": stamp,
            }
            continue
        state["logins"].setdefault(account.label, {})[ob.host] = {
            "seen_at": stamp,
            "cloud_ok": cloud_login(ob.probe_text) == login_ok,
        }
    return sentences


def unconsented_list(state: Mapping[str, Any]) -> list[dict[str, str]]:
    """Every login whose hash pair is in no account, with the host and both hashes."""
    return [
        {
            "host": host,
            "org_hash": str(v.get("org_hash") or ""),
            "email_hash": str(v.get("email_hash") or ""),
            "seen_at": str(v.get("seen_at") or ""),
            "sentence": (
                f"CLOUD-LOGIN host={host} org={v.get('org_hash')} email={v.get('email_hash')} "
                "has no consent row"
            ),
        }
        for host, v in sorted(state.get("unconsented", {}).items())
        if isinstance(v, dict)
    ]


def transports(
    rows: Sequence[ModelCloudTransportRow], accounts: Sequence[ModelCloudAccount]
) -> tuple[list[dict[str, Any]], list[str]]:
    """(the rows a start call may be made from, one sentence per row left out). Table order is kept. A row is left
    out before any probe when an account excludes it, it has ``push: false``, it is a contributor's machine, or it
    names no ssh target and is not the launching host. A host's lab availability is not a field of the row model."""
    excluded_by_registry = {h for a in accounts for h in a.transport_exclude}
    kept: list[dict[str, Any]] = []
    left: list[str] = []
    for row in rows:
        if row.name in excluded_by_registry:
            why = "listed in an account's transport_exclude"
        elif row.push is False:
            why = "push: false in its host table row (its logins push through their own GitHub connection)"
        elif row.contributor:
            why = "a contributor's machine"
        elif not row.target and not row.local:
            why = "its host table row names no ssh target"
        else:
            kept.append({"name": row.name, "target": row.target, "local": row.local})
            continue
        left.append(f"CLOUD-HOST {row.name} excluded: {why}")
    return kept, left


def _since(entries: Any, now: datetime, span: timedelta) -> dict[str, str]:
    floor = stamp_of(now - span)
    return (
        {str(k): str(v) for k, v in entries.items() if str(v) > floor}
        if isinstance(entries, dict)
        else {}
    )


def _merge_legacy(
    entries: Sequence[Mapping[str, Any]],
    now: datetime,
    ttl: timedelta,
    runs_kept: int,
    hold_stamp: str,
) -> dict[str, Any]:
    """One account entry from the old host entries of its ``legacy_hosts``: newest credit reading, latest recorded
    hold (a hold without the recorded stamp is dropped), the lower cap, the union of active runs."""
    out: dict[str, Any] = {}
    credits = [e["credit"] for e in entries if isinstance(e.get("credit"), dict)]
    if credits:
        newest = max(credits, key=lambda c: str(c.get("read_at") or ""))
        out["credit"] = dict(newest)
        remaining = newest.get("remaining")
        if isinstance(remaining, (int, float)) and remaining <= 0:
            out["exhausted_at"] = str(newest.get("read_at") or stamp_of(now))
    holds = [
        e for e in entries if recorded_hold(e, hold_stamp) and e.get("limited_until")
    ]
    if holds:
        hold = max(holds, key=lambda e: str(e["limited_until"]))
        out["limited_until"] = hold["limited_until"]
        out["limited_source"] = hold_stamp
        if hold.get("limited_why"):
            out["limited_why"] = hold["limited_why"]
    caps = [e["cap"] for e in entries if isinstance(e.get("cap"), int)]
    if caps:
        out["cap"] = min(caps)
    active: dict[str, str] = {}
    for e in entries:
        for run, at in _since(e.get("active"), now, ttl).items():
            active[run] = max(at, active.get(run, ""))
    if active:
        out["active"] = active
    refused = [e for e in entries if e.get("refused_at")]
    if refused:
        last = max(refused, key=lambda e: str(e["refused_at"]))
        out["refused_at"] = last["refused_at"]
        out["refused_why"] = last.get("refused_why", "")
    rates: list[dict[str, Any]] = [
        e["rate"] for e in entries if isinstance(e.get("rate"), dict)
    ]
    if rates:
        newest_rate = max(rates, key=lambda r: str(r.get("read_at") or ""))
        out["rate"] = dict(newest_rate)
    runs = [r for e in entries for r in (e.get("runs") or []) if isinstance(r, dict)]
    if runs:
        out["runs"] = runs[-runs_kept:]
    started = [str(e["started_ok_at"]) for e in entries if e.get("started_ok_at")]
    if started:
        out["started_ok_at"] = max(started)
    return out


def import_legacy(
    old: Mapping[str, Any],
    accounts: Sequence[ModelCloudAccount],
    now: datetime,
    *,
    ttl: timedelta,
    runs_kept: int,
    hold_stamp: str,
) -> dict[str, Any]:
    """The v2 state made from the host-keyed state file, once. Raises ValueError naming the host when an entry for a
    host outside every ``legacy_hosts`` carries an unexpired recorded hold or an active run; its other entries are
    dropped."""
    found = old.get("accounts")
    legacy: dict[str, Any] = found if isinstance(found, dict) else {}
    owner = {h: a.label for a in accounts for h in a.legacy_hosts}
    groups: dict[str, list[Mapping[str, Any]]] = {}
    for host, entry in sorted(legacy.items()):
        if not isinstance(entry, dict):
            continue
        label = owner.get(host)
        if label is not None:
            groups.setdefault(label, []).append(entry)
            continue
        until = str(entry.get("limited_until") or "")
        if entry.get("limited_source") == hold_stamp and until > stamp_of(now):
            raise ValueError(
                f"cloud-accounts.json: host {host} holds a recorded hold until {until} and no account lists "
                "it in legacy_hosts"
            )
        if _since(entry.get("active"), now, ttl):
            raise ValueError(
                f"cloud-accounts.json: host {host} has an active cloud run and no account lists it in legacy_hosts"
            )
    state = new_state()
    for label, entries in sorted(groups.items()):
        merged = _merge_legacy(entries, now, ttl, runs_kept, hold_stamp)
        if merged:
            state["accounts"][label] = merged
    last = owner.get(str(old.get("last") or ""))
    if last:
        state["last"] = last
    return state


def consent_reason(account: ModelCloudAccount, now: datetime) -> str | None:
    """Why the account's consent does not hold now: not a ledger row stamp, or ``consent_expires`` reached (the
    consent is valid strictly before it)."""
    if not CONSENT_RE.fullmatch(account.consent):
        return f"its consent {account.consent!r} is not a ledger row stamp"
    ends = parse_moment(account.consent_expires) if account.consent_expires else None
    if ends is not None and now >= ends:
        return f"consent ended {account.consent_expires}"
    return None


def credit_date_reason(account: ModelCloudAccount, now: datetime) -> str | None:
    """Why the account's credit date has lapsed (valid through ``credit_expires``, inclusive; a bare date is valid
    to the end of that day), or None."""
    ends = parse_moment(account.credit_expires) if account.credit_expires else None
    if ends is None:
        return None
    if len(account.credit_expires) == len("2026-01-01"):
        ends += timedelta(days=1) - timedelta(seconds=1)
    return f"credit expired {account.credit_expires}" if now > ends else None
