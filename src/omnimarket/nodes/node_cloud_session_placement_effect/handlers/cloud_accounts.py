# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Cloud account consent, concurrency, credit and placement state."""

from __future__ import annotations

import fcntl
import json
import re
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

CLOUD_STATE_FILE = "cloud-accounts.json"


# Account writes use their own lock so pool reads cannot delay them.
CLOUD_LOCK_FILE = "cloud-accounts.lock"
CLOUD_RUNS_KEPT = 50


# Successful starts raise concurrency; platform refusals lower it. No daily bound.
CLOUD_CAP_START = 4
CLOUD_CAP_CEILING = 24
CLOUD_REFUSAL_HOLD = timedelta(hours=1)
CLOUD_ACTIVE_TTL = timedelta(hours=4)


# Promotional credit is separate from the subscription limits shared with local work.
CLOUD_PROMOTIONAL = "ccr_promotional"
CLOUD_RATE_OK = frozenset({"allowed", "allowed_warning"})
CLOUD_LOGIN_OK, CLOUD_LOGIN_NONE, CLOUD_LOGIN_UNREADABLE = (
    "ok",
    "no-claude-ai-login",
    "unreadable",
)
CLOUD_CREDIT_UNREADABLE = (
    "unreadable: Anthropic documents no programmatic read of an account's cloud credit "
    "(the balance is on claude.ai/settings/usage); record a reading with cloud-credit"
)

CLAUDE_ENGINES = ("claude_sonnet", "claude_opus")


# Consent is declared by the caller, never discovered by probing a login.
CLOUD_CONSENT_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
CLOUD_NO_PUSH = (
    "push: false in its lab host table row: a cloud session pushes and opens pull requests through the "
    "login's own GitHub connection, never the lab bot"
)
CLOUD_NO_CONSENT = "its lab host table row names no cloud consent row (cloud_consent)"
CLOUD_CONTRIBUTOR = (
    "a contributor's own machine, whose logins are that person's: never a cloud account unless its lab "
    "host table row names a consent row (cloud_consent)"
)


def cloud_consent_refusal(host: Any) -> str | None:
    """Why the host's login is not a cloud account, or None when its table row names a consent row's stamp."""
    if getattr(host, "push", True) is False:
        return CLOUD_NO_PUSH
    consent = str(getattr(host, "cloud_consent", None) or "")
    if CLOUD_CONSENT_RE.fullmatch(consent):
        return None
    if getattr(host, "contributor", False):
        return CLOUD_CONTRIBUTOR
    if consent:
        return f"its cloud_consent {consent!r} is not a ledger row stamp"
    return CLOUD_NO_CONSENT


def consented_accounts(hosts: Sequence[Any]) -> frozenset[str]:
    """The pool hosts whose table rows consent to their logins starting cloud sessions."""
    return frozenset(h.name for h in hosts if cloud_consent_refusal(h) is None)


CLOUD_SESSION_RE = re.compile(r"\b((?:session|cse)_[A-Za-z0-9]{8,})\b")
CLOUD_URL_RE = re.compile(r"https://claude\.ai/code/(?:session|cse)_[A-Za-z0-9]+")


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
def _cloud_state(pdir: Path) -> Iterator[dict[str, Any]]:
    """The accounts file, read and written under its own lock (never the placement lock)."""
    pdir.mkdir(parents=True, exist_ok=True)
    path = pdir / CLOUD_STATE_FILE
    with (pdir / CLOUD_LOCK_FILE).open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
        if not isinstance(state, dict):
            state = {}
        state.setdefault("accounts", {})
        yield state
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(path)


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
    """Why an account starts no cloud session now, or None."""
    if account.get("exhausted_at"):
        return f"credit read zero at {account['exhausted_at']}"
    until = str(account.get("limited_until") or "")
    if until and until > f"{now:%Y-%m-%dT%H:%M:%SZ}":
        why = account.get("limited_why")
        return f"limited until {until}" + (f" ({why})" if why else "")
    if cloud_account_free(account, now) < 1:
        return f"at its cap: {cloud_account_running(account, now)} running (cap {cloud_account_cap(account)})"
    return None


def cloud_slots(
    pdir: Path, now: datetime | None = None, *, consented: Collection[str]
) -> dict[str, int]:
    """{account: sessions it may start now} over the ``consented`` accounts (``consented_accounts``) whose login last
    reached cloud sessions; an account never probed is not counted (a cloud lane's pick probes every account in turn),
    and one without consent is not counted whatever the accounts file says of it."""
    now = now or datetime.now(UTC)
    with _cloud_state(pdir) as state:
        accounts = dict(state["accounts"])
    return {
        name: (0 if cloud_account_skip(a, now) else cloud_account_free(a, now))
        for name, a in sorted(accounts.items())
        if name in consented
        and isinstance(a, dict)
        and a.get("login") == CLOUD_LOGIN_OK
    }


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
    """Record the rate-limit reading a session of the account carried (``rateLimitType``, ``status``,
    ``resetsAt``). An account whose reading is not allowed, or that draws on a limit other than the promotional cloud
    credit at a warning, starts no session until that limit resets. Returns why it is held, or None."""
    if not isinstance(rate, Mapping):
        return None
    now = now or datetime.now(UTC)
    kind, status = str(rate.get("rateLimitType") or ""), str(rate.get("status") or "")
    resets = rate.get("resetsAt")
    until = (
        f"{datetime.fromtimestamp(float(resets), UTC):%Y-%m-%dT%H:%M:%SZ}"
        if isinstance(resets, (int, float)) and resets > 0
        else f"{now + timedelta(hours=1):%Y-%m-%dT%H:%M:%SZ}"
    )
    why = None
    if status and status not in CLOUD_RATE_OK:
        why = f"{kind or 'rate'} limit {status}"
    elif kind and kind != CLOUD_PROMOTIONAL and status == "allowed_warning":
        why = (
            f"draws on its {kind} subscription limit at a warning, not the cloud credit"
        )
    with _cloud_state(pdir) as state:
        entry = state["accounts"].setdefault(account, {})
        entry["rate"] = {
            "type": kind,
            "status": status,
            "resets_at": until,
            "read_at": f"{now:%Y-%m-%dT%H:%M:%SZ}",
        }
        if why:
            entry["limited_until"], entry["limited_why"] = until, why
    return why


# Reuse fresh verdicts: network probes can delay a pick until its ordering is stale.
CLOUD_LOGIN_FRESH = timedelta(minutes=30)


def _login_fresh(entry: Mapping[str, Any], now: datetime) -> bool:
    at = str(entry.get("probed_at") or "")
    return bool(at) and at > f"{now - CLOUD_LOGIN_FRESH:%Y-%m-%dT%H:%M:%SZ}"


def cloud_pick(
    pdir: Path,
    hosts: Sequence[Any],
    probe: Callable[[Any], str],
    *,
    now: datetime | None = None,
    run_id: str = "",
    claude_engines: Collection[str] = CLAUDE_ENGINES,
) -> tuple[Any, list[dict[str, str]]]:
    """(the host whose Claude login starts this session, or None; every account tried). Accounts are the pool hosts'
    own logins whose lab host table rows name a consent row (``cloud_consent_refusal``; any other host is skipped
    before its probe): the one with the most free slots first, round-robin after the account the last cloud run used among
    equals. An account whose credit read zero or that is limited or at its cap is skipped; one whose login was read
    inside CLOUD_LOGIN_FRESH is taken at that verdict (no probe), and any other is probed, recorded, and passed over
    when it cannot reach cloud sessions. The account picked holds one slot for ``run_id`` from here, taken under the
    accounts lock so two runners at once cannot both take its last one; a runner that finds the slot taken meanwhile
    goes on to the next account."""
    now = now or datetime.now(UTC)
    stamp = f"{now:%Y-%m-%dT%H:%M:%SZ}"
    claude_hosts = sorted(
        (
            h
            for h in hosts
            if getattr(h, "engines_live", False)
            or set(getattr(h, "engines", ())) & set(claude_engines)
        ),
        key=lambda h: h.name,
    )
    with _cloud_state(pdir) as state:
        last, accounts = str(state.get("last") or ""), dict(state["accounts"])
    names = [h.name for h in claude_hosts]
    start = names.index(last) + 1 if last in names else 0
    order = claude_hosts[start:] + claude_hosts[:start]
    tried: list[dict[str, str]] = []
    candidates = []
    for host in order:
        entry = accounts.get(host.name) or {}
        skip = cloud_consent_refusal(host) or cloud_account_skip(entry, now)
        if skip:
            tried.append({"account": host.name, "skipped": skip})
        elif _login_fresh(entry, now) and entry.get("login") != CLOUD_LOGIN_OK:
            tried.append(
                {
                    "account": host.name,
                    "login": str(entry.get("login")),
                    "read_at": str(entry["probed_at"]),
                }
            )
        else:
            candidates.append(host)
    rank = {h.name: i for i, h in enumerate(order)}
    candidates.sort(
        key=lambda h: (
            -cloud_account_free(accounts.get(h.name) or {}, now),
            rank[h.name],
        )
    )
    pick = None
    for host in candidates:
        entry = accounts.get(host.name) or {}
        probed = not (_login_fresh(entry, now) and entry.get("login") == CLOUD_LOGIN_OK)
        verdict = probe(host) if probed else CLOUD_LOGIN_OK
        tried.append({"account": host.name, "login": verdict})
        with _cloud_state(pdir) as state:
            entry = state["accounts"].setdefault(host.name, {})
            if probed:
                entry.update(login=verdict, probed_at=stamp)
            if verdict != CLOUD_LOGIN_OK:
                continue
            late = cloud_account_skip(entry, now)
            if late:
                tried.append({"account": host.name, "skipped": late})
                continue
            state["last"] = host.name
            if run_id:
                entry["active"] = {
                    **_since(entry.get("active"), now, CLOUD_ACTIVE_TTL),
                    run_id: stamp,
                }
            pick = host
        break
    return pick, tried


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
        if why:
            entry["limited_why"] = why
        else:
            entry.pop("limited_why", None)


def cloud_caps(
    pdir: Path, now: datetime | None = None, *, consented: Collection[str]
) -> dict[str, dict[str, Any]]:
    """{account: {running, cap, held}} over the ``consented`` accounts whose login last reached cloud sessions."""
    now = now or datetime.now(UTC)
    with _cloud_state(pdir) as state:
        accounts = dict(state["accounts"])
    return {
        name: {
            "running": cloud_account_running(a, now),
            "cap": cloud_account_cap(a),
            "held": cloud_account_skip(a, now)
            if cloud_account_free(a, now) > 0
            else None,
        }
        for name, a in sorted(accounts.items())
        if name in consented
        and isinstance(a, dict)
        and a.get("login") == CLOUD_LOGIN_OK
    }
