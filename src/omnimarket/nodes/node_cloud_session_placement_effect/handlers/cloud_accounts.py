# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Cloud account consent, concurrency, credit and placement state."""

from __future__ import annotations

import fcntl
import json
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from omnimarket.config.service_endpoints import service_endpoint_url
from omnimarket.models.cloud_session_placement import (
    ModelCloudAccount,
    ModelCloudTransport,
)

from . import cloud_registry as reg

CLOUD_STATE_FILE = "cloud-accounts-v2.json"
CLOUD_LEGACY_STATE_FILE = "cloud-accounts.json"
CLOUD_LEGACY_IMPORTED = "cloud-accounts.json.imported"


# Account writes use their own lock so pool reads cannot delay them.
CLOUD_LOCK_FILE = "cloud-accounts.lock"
CLOUD_RUNS_KEPT = 50


# Successful starts raise concurrency; platform refusals lower it. No daily bound.
CLOUD_CAP_START = 4
CLOUD_CAP_CEILING = 24
CLOUD_REFUSAL_HOLD = timedelta(hours=1)
CLOUD_ACTIVE_TTL = timedelta(hours=4)


# A hold is stamped with where it came from; only a deliberate or measured hold gates placement.
CLOUD_HOLD_STAMP = "recorded"
CLOUD_LOGIN_OK, CLOUD_LOGIN_NONE, CLOUD_LOGIN_UNREADABLE = (
    "ok",
    "no-claude-ai-login",
    "unreadable",
)
CLOUD_CREDIT_UNREADABLE = (
    "unreadable: Anthropic documents no programmatic read of an account's cloud credit "
    "(the balance is on claude.ai/settings/usage); record a reading with cloud-credit"
)

CLOUD_SESSION_RE = re.compile(r"\b((?:session|cse)_[A-Za-z0-9]{8,})\b")
CLOUD_URL_RE = re.compile(
    re.escape(service_endpoint_url("claude.web_url"))
    + r"/code/(?:session|cse)_[A-Za-z0-9]+"
)


def cloud_login(stdout: str) -> str:
    """The probe's verdict: an id-shape or not-found answer means the login reached the sessions API."""
    low = stdout.lower()
    if "invalid session id" in low or "session not found" in low:
        return CLOUD_LOGIN_OK
    if any(
        m in low
        for m in (
            "sign-in",
            "/login",
            "session expired",
            "organization uuid",
            "aren't available",
            "disabled by your organization",
        )
    ):
        return CLOUD_LOGIN_NONE
    return CLOUD_LOGIN_UNREADABLE


def parse_cloud_start(stdout: str, session_url_base: str) -> dict[str, str] | None:
    """The new session's id and URL from the CLI's answer (its JSON form, else its text form), or None. A URL the
    answer does not name is ``session_url_base`` and the id; the caller reads the base from its overlay."""
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                data = json.loads(line)
            except ValueError:
                continue
            sid = str(data.get("session_id") or "")
            if data.get("ok") is not False and CLOUD_SESSION_RE.fullmatch(sid):
                return {
                    "session_id": sid,
                    "url": str(data.get("url") or f"{session_url_base}{sid}"),
                }
    url = CLOUD_URL_RE.search(stdout)
    match = CLOUD_SESSION_RE.search(url.group(0) if url else stdout)
    if match and (url or "session id" in stdout.lower()):
        return {
            "session_id": match.group(1),
            "url": url.group(0) if url else f"{session_url_base}{match.group(1)}",
        }
    return None


@contextmanager
def _cloud_state(
    pdir: Path,
    accounts: Sequence[ModelCloudAccount] | None = None,
    now: datetime | None = None,
) -> Iterator[dict[str, Any]]:
    """The accounts file, read and written under its own lock (never the placement lock). The first call that has the
    registry (``accounts`` not None) imports the old host-keyed file once and renames it; before that import a call
    without the registry is refused, so no write can strand the old file."""
    pdir.mkdir(parents=True, exist_ok=True)
    path = pdir / CLOUD_STATE_FILE
    legacy = pdir / CLOUD_LEGACY_STATE_FILE
    with (pdir / CLOUD_LOCK_FILE).open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        imported = False
        if path.exists():
            try:
                state = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                state = {}
        elif legacy.exists():
            if accounts is None:
                raise ValueError(
                    f"{CLOUD_LEGACY_STATE_FILE} awaits its one-time import: call with the registry (accounts)"
                )
            old = json.loads(legacy.read_text(encoding="utf-8"))
            state = reg.import_legacy(
                old if isinstance(old, dict) else {},
                accounts,
                now or datetime.now(UTC),
                ttl=CLOUD_ACTIVE_TTL,
                runs_kept=CLOUD_RUNS_KEPT,
                hold_stamp=CLOUD_HOLD_STAMP,
            )
            imported = True
        else:
            state = {}
        if not isinstance(state, dict):
            state = {}
        for key, empty in reg.new_state().items():
            state.setdefault(key, empty)
        yield state
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(path)
        if imported:
            legacy.replace(pdir / CLOUD_LEGACY_IMPORTED)


def _since(entries: Any, now: datetime, span: timedelta) -> dict[str, str]:
    """{run id: stamp} of the entries stamped within ``span`` before ``now``."""
    floor = f"{now - span:%Y-%m-%dT%H:%M:%SZ}"
    return (
        {str(k): str(v) for k, v in (entries or {}).items() if str(v) > floor}
        if isinstance(entries, dict)
        else {}
    )


def cloud_account_cap(account: Mapping[str, Any]) -> int:
    """The sessions the account may run at once now: its learned cap, or CLOUD_CAP_START."""
    cap = account.get("cap")
    return (
        max(1, min(CLOUD_CAP_CEILING, cap)) if isinstance(cap, int) else CLOUD_CAP_START
    )


def cloud_account_running(account: Mapping[str, Any], now: datetime) -> int:
    return len(_since(account.get("active"), now, CLOUD_ACTIVE_TTL))


def cloud_account_free(account: Mapping[str, Any], now: datetime) -> int:
    """How many more sessions the account may start now under its concurrency cap (there is no daily bound)."""
    return max(0, cloud_account_cap(account) - cloud_account_running(account, now))


def cloud_account_skip(account: Mapping[str, Any], now: datetime) -> str | None:
    """Why an account starts no cloud session now, or None. Cloud sessions draw on the cloud credit, so a
    subscription rate reading never gates: only the credit, a recorded hold (a start the platform refused, or
    ``mark_cloud_limited``) and the concurrency cap do."""
    if account.get("exhausted_at"):
        return f"credit read zero at {account['exhausted_at']}"
    until = str(account.get("limited_until") or "")
    if (
        until
        and until > f"{now:%Y-%m-%dT%H:%M:%SZ}"
        and account.get("limited_source") == CLOUD_HOLD_STAMP
    ):
        why = account.get("limited_why")
        return f"limited until {until}" + (f" ({why})" if why else "")
    if cloud_account_free(account, now) < 1:
        return f"at its cap: {cloud_account_running(account, now)} running (cap {cloud_account_cap(account)})"
    return None


def release_cloud_slot(pdir: Path, account: str, run_id: str) -> None:
    """The run no longer holds the account's slot."""
    with _cloud_state(pdir) as state:
        (state["accounts"].setdefault(account, {}).get("active") or {}).pop(
            run_id, None
        )


def note_cloud_start(pdir: Path, account: str, *, now: datetime | None = None) -> int:
    """A session started: an account that was at its cap (this session included) and has no refusal inside
    CLOUD_REFUSAL_HOLD may run one more. Returns the cap now."""
    now = now or datetime.now(UTC)
    with _cloud_state(pdir) as state:
        entry = state["accounts"].setdefault(account, {})
        cap = cloud_account_cap(entry)
        refused = str(entry.get("refused_at") or "")
        held = (
            bool(refused) and refused > f"{now - CLOUD_REFUSAL_HOLD:%Y-%m-%dT%H:%M:%SZ}"
        )
        if (
            cloud_account_running(entry, now) >= cap
            and not held
            and cap < CLOUD_CAP_CEILING
        ):
            cap += 1
        entry["cap"] = cap
        entry["started_ok_at"] = f"{now:%Y-%m-%dT%H:%M:%SZ}"
        return cap


# A refusal that names a subscription or usage limit holds the account; a concurrency refusal only lowers its cap.
CLOUD_SUBSCRIPTION_REFUSAL_RE = re.compile(
    r"usage limit|rate.?limit|limit reached|weekly limit|quota|\b429\b|subscription",
    re.IGNORECASE,
)
CLOUD_RESET_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")


def _refusal_reset(why: str, now: datetime) -> str:
    """The reset a refusal names (a later stamp in its text), else ``now`` plus CLOUD_REFUSAL_HOLD."""
    stamp = f"{now:%Y-%m-%dT%H:%M:%SZ}"
    named = [m for m in CLOUD_RESET_RE.findall(why) if m > stamp]
    return max(named) if named else f"{now + CLOUD_REFUSAL_HOLD:%Y-%m-%dT%H:%M:%SZ}"


def note_cloud_refusal(
    pdir: Path, account: str, run_id: str, why: str, *, now: datetime | None = None
) -> int:
    """The platform refused a start: the account's cap falls to the sessions it runs now (this run's slot not
    counted), at least one, and is held there for CLOUD_REFUSAL_HOLD. Returns the cap now."""
    now = now or datetime.now(UTC)
    with _cloud_state(pdir) as state:
        entry = state["accounts"].setdefault(account, {})
        active = _since(entry.get("active"), now, CLOUD_ACTIVE_TTL)
        active.pop(run_id, None)
        entry["active"] = active
        entry["cap"] = max(1, min(cloud_account_cap(entry), len(active)))
        entry["refused_at"] = f"{now:%Y-%m-%dT%H:%M:%SZ}"
        entry["refused_why"] = why[-300:]
        if CLOUD_SUBSCRIPTION_REFUSAL_RE.search(why):
            entry["limited_until"] = _refusal_reset(why, now)
            entry["limited_why"] = why[-300:]
            entry["limited_source"] = CLOUD_HOLD_STAMP
        return int(entry["cap"])


CLOUD_REFUSAL_RE = re.compile(
    r"too many|concurrent|rate.?limit|usage limit|limit reached|quota|capacity|\b429\b|"
    r"try again later|exceeded",
    re.IGNORECASE,
)


def cloud_start_refused(answer: str) -> bool:
    return bool(CLOUD_REFUSAL_RE.search(answer))


def record_cloud_rate(
    pdir: Path,
    account: str,
    rate: Mapping[str, Any] | None,
    *,
    now: datetime | None = None,
) -> str | None:
    """Record the subscription rate reading a session of the account carried (``rateLimitType``, ``status``,
    ``resetsAt``) as telemetry. A cloud session draws on the cloud credit, never on that subscription limit, so no
    reading holds the account: the answer is always None (a hold comes from a refused start, ``note_cloud_refusal``)."""
    if not isinstance(rate, Mapping):
        return None
    now = now or datetime.now(UTC)
    resets = rate.get("resetsAt")
    until = (
        f"{datetime.fromtimestamp(float(resets), UTC):%Y-%m-%dT%H:%M:%SZ}"
        if isinstance(resets, (int, float)) and resets > 0
        else f"{now + timedelta(hours=1):%Y-%m-%dT%H:%M:%SZ}"
    )
    with _cloud_state(pdir) as state:
        state["accounts"].setdefault(account, {})["rate"] = {
            "type": str(rate.get("rateLimitType") or ""),
            "status": str(rate.get("status") or ""),
            "resets_at": until,
            "read_at": f"{now:%Y-%m-%dT%H:%M:%SZ}",
        }
    return None


@dataclass(frozen=True)
class AccountView:
    """One registered account judged at ``now``: ``gate`` is why it is not counted at all (consent, credit date, no
    usable login), ``skip`` any reason it starts nothing now (the gate, else a zero credit, a hold or a full cap)."""

    account: ModelCloudAccount
    gate: str | None
    skip: str | None
    free: int
    running: int
    cap: int
    transports: tuple[str, ...]
    stale_hosts: tuple[str, ...]
    last_seen: str

    @property
    def label(self) -> str:
        return self.account.label


def _login_reason(
    account: ModelCloudAccount,
    held: Mapping[str, Any],
    order: Sequence[str],
    now: datetime,
    fresh_s: int,
) -> tuple[tuple[str, ...], tuple[str, ...], str | None]:
    """(hosts whose login is fresh and passed the cloud probe in preference order, hosts whose login is stale, why
    there is none)."""
    floor = reg.stamp_of(now - timedelta(seconds=fresh_s))
    usable = [h for h in order if h in held and h not in account.transport_exclude]
    if not usable:
        if held:
            return (
                (),
                (),
                (
                    "its logins are on hosts that start no cloud session ("
                    + ", ".join(sorted(held))
                    + ")"
                ),
            )
        return (), (), "no host logged into it has been seen"
    seen = {h: str(held[h].get("seen_at") or "") for h in usable}
    fresh = [h for h in usable if seen[h] > floor]
    stale = tuple(h for h in usable if seen[h] <= floor)
    ok = tuple(h for h in fresh if held[h].get("cloud_ok"))
    if ok:
        return ok, stale, None
    if fresh:
        return (
            (),
            stale,
            f"its login on {fresh[0]} failed the cloud probe at {seen[fresh[0]]}",
        )
    newest = max(usable, key=lambda h: seen[h])
    return (
        (),
        stale,
        (
            f"its login was last seen {seen[newest]} on {newest}, older than {fresh_s} seconds"
        ),
    )


def evaluate_accounts(
    state: Mapping[str, Any],
    accounts: Sequence[ModelCloudAccount],
    transports: Sequence[ModelCloudTransport] | None,
    now: datetime,
    fresh_s: int,
) -> list[AccountView]:
    """Every account in registry order, judged. A host's lab availability is not an input anywhere here."""
    order = [t.name for t in transports] if transports is not None else None
    views: list[AccountView] = []
    for a in accounts:
        entry = (
            (state["accounts"].get(a.label) or {})
            if isinstance(state["accounts"], dict)
            else {}
        )
        held = state["logins"].get(a.label) or {}
        hosts = order if order is not None else sorted(held)
        fresh, stale, login_why = _login_reason(a, held, hosts, now, fresh_s)
        gate = reg.consent_reason(a, now) or reg.credit_date_reason(a, now) or login_why
        running, cap = cloud_account_running(entry, now), cloud_account_cap(entry)
        free = cloud_account_free(entry, now)
        skip = gate
        if skip is None:
            skip = _state_skip(entry, now)
        seen = [
            str(v.get("seen_at") or "") for v in held.values() if isinstance(v, dict)
        ]
        views.append(
            AccountView(
                a, gate, skip, free, running, cap, fresh, stale, max(seen, default="")
            )
        )
    return views


def _state_skip(entry: Mapping[str, Any], now: datetime) -> str | None:
    """The usage state's reason an account starts nothing, with its date: a zero credit, a recorded hold, a full cap."""
    if entry.get("exhausted_at"):
        return f"credit read zero at {entry['exhausted_at']}"
    held = cloud_account_skip(entry, now)
    if held and held.startswith("limited until"):
        return held
    if cloud_account_free(entry, now) < 1:
        active = _since(entry.get("active"), now, CLOUD_ACTIVE_TTL)
        frees = reg.parse_moment(min(active.values())) if active else None
        when = (
            f"; a slot frees by {reg.stamp_of(frees + CLOUD_ACTIVE_TTL)}"
            if frees
            else ""
        )
        return f"at its cap: {cloud_account_running(entry, now)} running (cap {cloud_account_cap(entry)}){when}"
    return None


def _flags(views: Sequence[AccountView], state: Mapping[str, Any]) -> list[str]:
    return [
        f"CLOUD-ACCOUNT {v.label} flag: no credit reading is recorded (record one with cloud-credit)"
        for v in views
        if v.gate is None
        and not isinstance((state["accounts"].get(v.label) or {}).get("credit"), dict)
    ]


def _sentences(views: Sequence[AccountView]) -> list[str]:
    return [f"CLOUD-ACCOUNT {v.label} skipped: {v.skip}" for v in views if v.skip]


def cloud_slots(
    pdir: Path,
    accounts: Sequence[ModelCloudAccount],
    transports: Sequence[ModelCloudTransport] | None,
    *,
    fresh_s: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    """{accounts: {label: sessions it may start now}, caps, sentences, flags, unconsented} over the accounts that are
    consented, inside their credit date and have a fresh login (an account the registry does not name is not
    counted whatever the accounts file says of it)."""
    now = now or datetime.now(UTC)
    with _cloud_state(pdir, accounts, now) as state:
        views = evaluate_accounts(state, accounts, transports, now, fresh_s)
        counted = [v for v in views if v.gate is None]
        entries = {v.label: state["accounts"].get(v.label) or {} for v in views}
        sentences = _sentences(views) or ([] if accounts else [reg.REGISTRY_EMPTY])
        sentences += [
            f"CLOUD-ACCOUNT {v.label} startable: {v.free} free (cap {v.cap}) via {v.transports[0]}"
            for v in counted
            if v.skip is None
        ]
        return {
            "accounts": {v.label: (0 if v.skip else v.free) for v in counted},
            "caps": {
                v.label: {
                    "running": v.running,
                    "cap": v.cap,
                    "held": cloud_account_skip(entries[v.label], now)
                    if v.free > 0
                    else None,
                }
                for v in counted
            },
            "sentences": sentences,
            "flags": _flags(views, state),
            "unconsented": reg.unconsented_list(state),
        }


def cloud_pick(
    pdir: Path,
    accounts: Sequence[ModelCloudAccount],
    transports: Sequence[ModelCloudTransport],
    *,
    fresh_s: int,
    now: datetime | None = None,
    run_id: str = "",
) -> dict[str, Any]:
    """Pick by account, then transport. The account with the most free slots among those that start now wins,
    round-robin after the last one picked among equals; its transport is the first host in ``transports`` order whose
    login is fresh and passed the cloud probe. The pick holds one slot for ``run_id`` from here, taken under the
    accounts lock so two runners at once cannot both take the last one. No probe happens here: the logins were
    observed (``observe_logins``), and a stale login asks the caller to observe again (``needs_probe``)."""
    now = now or datetime.now(UTC)
    stamp = reg.stamp_of(now)
    with _cloud_state(pdir, accounts, now) as state:
        views = evaluate_accounts(state, accounts, transports, now, fresh_s)
        names = [a.label for a in accounts]
        last = str(state.get("last") or "")
        begin = names.index(last) + 1 if last in names else 0
        rank = {n: (i - begin) % max(1, len(names)) for i, n in enumerate(names)}
        ready = sorted(
            (v for v in views if v.skip is None), key=lambda v: (-v.free, rank[v.label])
        )
        sentences = _sentences(views) or ([] if accounts else [reg.REGISTRY_EMPTY])
        tried = [{"account": v.label, "skipped": v.skip or ""} for v in views if v.skip]
        picked = ready[0] if ready else None
        by_name = {t.name: t for t in transports}
        chosen: list[dict[str, Any]] = []
        if picked is not None:
            chosen = [by_name[h].model_dump() for h in picked.transports]
            tried.append({"account": picked.label, "transport": picked.transports[0]})
            state["last"] = picked.label
            if run_id:
                entry = state["accounts"].setdefault(picked.label, {})
                entry["active"] = {
                    **_since(entry.get("active"), now, CLOUD_ACTIVE_TTL),
                    run_id: stamp,
                }
        stale = sorted(
            {h for v in views if v.gate and v.stale_hosts for h in v.stale_hosts}
        )
        return {
            "account": picked.label if picked else None,
            "transport": picked.transports[0] if picked else None,
            "transports": chosen,
            "tried": tried,
            "sentences": sentences,
            "flags": _flags(views, state),
            "unconsented": reg.unconsented_list(state),
            "stale_hosts": [] if picked else stale,
            "needs_probe": None if picked or not stale else ",".join(stale),
        }


def cloud_observe_logins(
    pdir: Path,
    accounts: Sequence[ModelCloudAccount],
    observations: Sequence[Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Record the login map from the raw texts of one tick, each observation replacing its host's entry."""
    now = now or datetime.now(UTC)
    with _cloud_state(pdir, accounts, now) as state:
        sentences = reg.observe_logins(
            state, accounts, observations, now, cloud_login, CLOUD_LOGIN_OK
        )
        return {
            "logins": {k: dict(v) for k, v in state["logins"].items() if v},
            "unconsented": reg.unconsented_list(state),
            "unreachable": sorted(state["unreachable"]),
            "sentences": sentences,
        }


def cloud_start_outcome(
    pdir: Path,
    account: str,
    run_id: str,
    transport: str,
    began: bool,
    text: str,
    untried: Sequence[str],
    session_url_base: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """What one start attempt means. ``began`` false: the call never started (ssh could not connect), so the next
    transport is tried and the account stays unheld. ``began`` true: a session in the answer is a start, a refusal
    holds or lowers the account, and anything else is ``start-unknown``: no second transport is tried and the slot is
    kept until its TTL, because the session may exist."""
    if not began:
        if untried:
            return {
                "verdict": "try-next",
                "next_transport": untried[0],
                "sentence": f"CLOUD-START {run_id} could not reach {transport}: trying {untried[0]}",
            }
        release_cloud_slot(pdir, account, run_id)
        return {
            "verdict": "failed",
            "sentence": f"CLOUD-START {run_id} could not reach {transport} and has no transport left",
        }
    session = parse_cloud_start(text, session_url_base)
    if session:
        return {
            "verdict": "started",
            "parsed": session,
            "cap": note_cloud_start(pdir, account, now=now),
        }
    if cloud_start_refused(text):
        return {
            "verdict": "refused",
            "cap": note_cloud_refusal(pdir, account, run_id, text, now=now),
            "sentence": f"CLOUD-START {run_id} refused by {account}: {text.strip()[-120:]}",
        }
    return {
        "verdict": "start-unknown",
        "sentence": f"start-unknown {run_id} unresolved",
    }


def run_start(
    pdir: Path,
    account: str,
    run_id: str,
    order: Sequence[str],
    ssh: Callable[[str], tuple[bool, str]],
    session_url_base: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Make the start call over the injected ``ssh`` seam (transport name -> (call began, raw answer)), one transport
    at a time in ``order``; only a failure before the call began passes to the next."""
    remaining = list(order)
    out: dict[str, Any] = {
        "verdict": "failed",
        "sentence": f"CLOUD-START {run_id} has no transport",
    }
    while remaining:
        transport = remaining.pop(0)
        began, text = ssh(transport)
        out = cloud_start_outcome(
            pdir,
            account,
            run_id,
            transport,
            began,
            text,
            tuple(remaining),
            session_url_base,
            now=now,
        )
        out["transport"] = transport
        if out["verdict"] != "try-next":
            return out
    return out


def record_cloud_credit(
    pdir: Path,
    account: str,
    remaining: float,
    source: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Record one reading of an account's remaining cloud credit; a reading of zero or less retires the account."""
    stamp = f"{(now or datetime.now(UTC)):%Y-%m-%dT%H:%M:%SZ}"
    with _cloud_state(pdir) as state:
        entry = state["accounts"].setdefault(account, {})
        entry["credit"] = {"remaining": remaining, "read_at": stamp, "source": source}
        if remaining <= 0:
            entry["exhausted_at"] = stamp
        else:
            entry.pop("exhausted_at", None)
        return dict(entry)


def cloud_credit(pdir: Path, account: str) -> dict[str, Any] | None:
    with _cloud_state(pdir) as state:
        credit = (state["accounts"].get(account) or {}).get("credit")
    return dict(credit) if isinstance(credit, dict) else None


def cloud_cost(
    before: Mapping[str, Any] | None, after: Mapping[str, Any] | None, started: str
) -> dict[str, Any]:
    """The run's credit used: the drop between a reading taken before the run and one taken after it started."""
    if (
        before
        and after
        and isinstance(before.get("remaining"), (int, float))
        and isinstance(after.get("remaining"), (int, float))
        and str(after.get("read_at")) >= started
        and str(before.get("read_at")) <= started
    ):
        return {
            "credit_used": round(
                float(before["remaining"]) - float(after["remaining"]), 2
            ),
            "credit_before": before,
            "credit_after": after,
        }
    return {
        "credit_used": None,
        "credit_before": before,
        "credit_after": after,
        "credit_read": CLOUD_CREDIT_UNREADABLE,
    }


def record_cloud_run(pdir: Path, account: str, run: Mapping[str, Any]) -> None:
    with _cloud_state(pdir) as state:
        entry = state["accounts"].setdefault(account, {})
        entry["runs"] = [*(entry.get("runs") or []), dict(run)][-CLOUD_RUNS_KEPT:]
        (entry.get("active") or {}).pop(str(run.get("run_id") or ""), None)


def mark_cloud_limited(pdir: Path, account: str, until: str, why: str = "") -> None:
    with _cloud_state(pdir) as state:
        entry = state["accounts"].setdefault(account, {})
        entry["limited_until"] = until
        entry["limited_source"] = CLOUD_HOLD_STAMP
        if why:
            entry["limited_why"] = why
        else:
            entry.pop("limited_why", None)
