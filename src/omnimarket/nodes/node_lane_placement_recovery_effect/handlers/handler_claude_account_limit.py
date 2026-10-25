# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Account-aware Claude quota recovery behind the existing placement recovery node (OMN-20864).

The calling adapter supplies its existing placement, credential-selection and capture ports. This handler owns the
shared-account decision, credential verification, notice consumption and recovery boundary; credentials stay on each host.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from ..models.model_claude_account_limit import (
    ModelClaudeAccountLimitRequest,
    ModelClaudeAccountLimitResult,
)

Capture = Callable[..., subprocess.CompletedProcess[str]]
_POOL_LABEL_RE = re.compile(
    r"^(?:ALARM )?claude-pool host=\S+ .* label=([a-z0-9][a-z0-9-]*)\s*$", re.MULTILINE
)
_TOKEN_ACCOUNT_RE = re.compile(
    r"^claude-auth account=((?:token|native)-[0-9a-f]{64})\s*$", re.MULTILINE
)


class HandlerClaudeAccountLimit:
    def __init__(self, runtime: Mapping[str, Any] | None = None) -> None:
        self.runtime = runtime if runtime is not None else {}

    def handle(
        self, request: ModelClaudeAccountLimitRequest
    ) -> ModelClaudeAccountLimitResult:
        if request.operation == "reprobe":
            if request.directory is None:
                raise ValueError("reprobe requires a placement directory")
            return ModelClaudeAccountLimitResult(
                limited=self.reprobe_limited(
                    request.directory,
                    request.hosts,
                    capture=request.capture,
                    now=request.now,
                    interval_s=request.interval_s,
                )
            )
        if request.operation == "passed":
            if request.state is None:
                raise ValueError("passed requires a state directory")
            return ModelClaudeAccountLimitResult(
                recovered_at=self._passed_at(request.state, request.account)
            )
        if request.operation == "probe":
            answered, account, identities = self._claude_room_verified(
                request.probe_host, request.capture or self.runtime["_capture"]
            )
            return ModelClaudeAccountLimitResult(
                has_room=answered, account=account, identities=frozenset(identities)
            )
        if request.operation == "revoke":
            return self.revoke_pool_token(
                request.host, request.text, capture=request.capture
            )
        if request.operation == "auth":
            return self.claude_auth_failed(
                request.host, request.text, capture=request.capture
            )
        if request.operation == "pool":
            return ModelClaudeAccountLimitResult(
                has_room=self.mark_pool_token(
                    request.host, request.text, request.seconds, capture=request.capture
                )
            )
        if request.operation == "mark":
            if request.directory is None:
                raise ValueError("mark requires a placement directory")
            return ModelClaudeAccountLimitResult(
                until=self.mark_claude_limited(
                    request.directory,
                    request.host,
                    request.text,
                    request.seconds,
                    capture=request.capture,
                    now=request.now,
                    lease_id=request.lease_id,
                    observed_at=request.observed_at,
                    decisions=request.decisions,
                )
            )
        raise ValueError(f"unknown Claude account operation: {request.operation}")

    def _passed_at(self, state: Path, account: str | None) -> datetime | None:
        recovery = self.runtime["_limit_json"](state / "passed.json")
        accounts = recovery.get("accounts")
        if isinstance(accounts, dict) and (account or "") in accounts:
            return cast(
                "datetime | None", self.runtime["_marker_time"](accounts[account or ""])
            )
        return (
            self.runtime["_marker_time"](recovery.get("at"))
            if recovery.get("account") == account
            else None
        )

    def _pool_account(self, text: str) -> str | None:
        labels = _POOL_LABEL_RE.findall(text)
        return labels[-1] if labels else None

    def _claude_account(self, text: str) -> str | None:
        label = self._pool_account(text)
        tokens = _TOKEN_ACCOUNT_RE.findall(text)
        return label or (tokens[-1] if tokens else None)

    def _claude_identities(self, text: str) -> set[str]:
        return set(_POOL_LABEL_RE.findall(text)) | set(_TOKEN_ACCOUNT_RE.findall(text))

    def _mark_pool_account(
        self, host: Any, label: str, seconds: float, capture: Capture
    ) -> int:
        script = f'{self.runtime["CLAUDE_POOL_SH"]}\n[ -n "$CPY" ] || exit 127\n"$CPY" -m {self.runtime["CLAUDE_POOL_MODULE"]} mark-limited --label {shlex.quote(label)} --seconds {int(seconds) + 1}'
        return capture(
            self.runtime["exec_argv"](host, "claude-pool-mark", script),
            timeout=self.runtime["PROBE_TIMEOUT_S"],
        ).returncode

    def _mark_native_limited(self, host: Any, capture: Capture) -> bool:
        script = 'K="$HOME/.cache/omni"; mkdir -p "$K" && touch "$K/claude-host-limited" && rm -f "$K/claude-host-ok"'
        return (
            capture(
                self.runtime["exec_argv"](host, "claude-native-limit", script),
                timeout=self.runtime["PROBE_TIMEOUT_S"],
            ).returncode
            == 0
        )

    def _mark_pool_revoked(
        self, host: Any, label: str, capture: Capture
    ) -> subprocess.CompletedProcess[str]:
        script = f'{self.runtime["CLAUDE_POOL_SH"]}\n[ -n "$CPY" ] || exit 127\n"$CPY" -m {self.runtime["CLAUDE_POOL_MODULE"]} mark-revoked --label {shlex.quote(label)}'
        return capture(
            self.runtime["exec_argv"](host, "claude-pool-revoke", script),
            timeout=self.runtime["PROBE_TIMEOUT_S"],
        )

    def _claude_room_verified(
        self, host: Any, capture: Capture
    ) -> tuple[bool, str | None, set[str]]:
        """Stored room is untried quota. Ask the exact selector used by a lane before admitting it."""
        tried: set[str] = set()
        identities: set[str] = set()
        remaining, first = (4, True)
        while remaining:
            remaining -= 1
            done = capture(
                self.runtime["exec_argv"](
                    host, "limit-reprobe", self.runtime["limit_reprobe_script"]()
                ),
                timeout=self.runtime["LIMIT_REPROBE_TIMEOUT_S"],
            )
            text = done.stdout or ""
            if first:
                count = re.search(
                    "^(?:ALARM )?claude-pool host=\\S+ tokens=(\\d+)\\b",
                    text,
                    re.MULTILINE,
                )
                if count is not None:
                    remaining = max(remaining, int(count[1]))
                first = False
            identities.update(self._claude_identities(text))
            if self.runtime["limit_probe_answered"](done.returncode, text):
                return (True, self._claude_account(text), identities)
            label = self._pool_account(text)
            account = self._claude_account(text)
            if self.runtime["auth_failure"](text) and label is not None:
                if label in tried:
                    return (False, account, identities)
                tried.add(label)
                if self._mark_pool_revoked(host, label, capture).returncode == 0:
                    remaining = max(remaining, 1)
                    continue
                return (False, account, identities)
            seconds = self.runtime["limit_backoff_s"](text)
            if seconds is None:
                return (False, account, identities)
            if label is None:
                if (
                    account is not None
                    and account.startswith("native-")
                    and (account not in tried)
                ):
                    tried.add(account)
                    if self._mark_native_limited(host, capture):
                        remaining = max(remaining, 1)
                        continue
                return (False, account, identities)
            if label in tried:
                return (False, self._claude_account(text), identities)
            tried.add(label)
            if self._mark_pool_account(host, label, seconds, capture) not in (0, 4):
                return (False, self._claude_account(text), identities)
        return (False, None, identities)

    def revoke_pool_token(
        self, host: str, text: str, *, capture: Capture | None = None
    ) -> ModelClaudeAccountLimitResult:
        label, pick = (
            self._pool_account(text),
            next((h for h in self.runtime["load_pool"]() if h.name == host), None),
        )
        if label is None or pick is None:
            return ModelClaudeAccountLimitResult()
        capture = capture or self.runtime["_capture"]
        done = self._mark_pool_revoked(pick, label, capture)
        alert = next(
            (
                line
                for line in (done.stderr or "").splitlines()
                if line.startswith("ALERT ")
            ),
            None,
        )
        has_room = done.returncode == 0 and self._claude_room_verified(pick, capture)[0]
        return ModelClaudeAccountLimitResult(
            has_room=has_room, account=label, alert=alert
        )

    def claude_auth_failed(
        self, host: str, text: str, *, capture: Capture | None = None
    ) -> ModelClaudeAccountLimitResult:
        """A Claude engine on ``host`` ended on an auth failure (OMN-17427): revoke the pool token it ran on, and mark the
        host auth-expired only when no other token answers. ``failure`` holds the lane failure's fields and its
        ``notices``, the runner's echo lines as (word, tail); a None tail is the failure's reason."""
        revoke = self.revoke_pool_token(host, text, capture=capture)
        notices: list[tuple[str, str | None]] = []
        failure: dict[str, Any] = {
            "until": None,
            "kind": "pool-token-revoked",
            "notices": notices,
        }
        if revoke.account is not None:
            failure["revoked_label"] = revoke.account
        if revoke.alert is not None:
            failure["alert"] = revoke.alert
            notices.append(
                (
                    "REMOTE-LANE-POOL-TOKEN-REVOKED",
                    f"label={revoke.account} {revoke.alert}",
                )
            )
        if not revoke.has_room:
            # The mark lasts until the operator clears it (AUTH_EXPIRY_S is only the safety bound).
            failure["kind"] = "host-auth-expired"
            failure["until"] = self.runtime["mark_auth_expired"](
                self.runtime["placement_dir"](), host
            )
            notices.append(("REMOTE-LANE-HOST-AUTH-EXPIRED", None))
        return replace(revoke, failure=failure)

    def mark_pool_token(
        self, host: str, text: str, seconds: float, *, capture: Capture | None = None
    ) -> bool:
        label, pick = (
            self._pool_account(text),
            next((h for h in self.runtime["load_pool"]() if h.name == host), None),
        )
        if label is None or pick is None or seconds <= 0:
            return False
        capture = capture or self.runtime["_capture"]
        return (
            self._mark_pool_account(pick, label, seconds, capture) in (0, 4)
            and self._claude_room_verified(pick, capture)[0]
        )

    def mark_claude_limited(
        self,
        directory: Path,
        host: str,
        text: str,
        seconds: float,
        *,
        capture: Capture | None = None,
        now: datetime | None = None,
        lease_id: int | None = None,
        observed_at: datetime | None = None,
        decisions: dict[str, dict[str, Any]] | None = None,
    ) -> str | None:
        """Bench exhausted Claude accounts across hosts with a known shared identity (OMN-20864).

        An unknown label or unreachable peer is not evidence it shares the account. A known peer
        stays placeable only after a real call on its remaining token answers, never on metadata alone.
        """
        capture, now = (capture or self.runtime["_capture"], now or datetime.now(UTC))
        label, account = (self._pool_account(text), self._claude_account(text))
        reset = self.runtime["_reset_in_s"](text, now)
        # Identified accounts share the notice's exact reset; unattributed host notices
        # retain the caller's backoff, including its reset safety margin.
        if reset is not None and account is not None:
            seconds = reset - self.runtime["RESET_MARGIN_S"]
        identities = self._claude_identities(text)
        hosts = {h.name: h for h in self.runtime["load_pool"]()}
        with self.runtime["_limit_lock"](directory, host) as state:
            consumed = lease_id is not None and (
                (state / f"{lease_id}.json").exists()
                or self.runtime["_limit_json"](
                    directory / f"{self.runtime['LIMITED_PREFIX']}{host}"
                ).get("lease_id")
                == lease_id
            )
            passed = self._passed_at(state, account)
            stale = passed is not None and (observed_at or now) <= passed
        if consumed or stale:
            if decisions is not None:
                decisions[host] = {
                    "decision": "consumed" if consumed else "stale",
                    "account": account,
                }
            return cast(
                "str | None",
                self.runtime["mark_limited"](
                    directory,
                    host,
                    seconds,
                    now=now,
                    lease_id=lease_id,
                    observed_at=observed_at,
                    account=account,
                ),
            )

        def bench(name: str) -> dict[str, Any] | None:
            with self.runtime["_limit_lock"](directory, name):
                current = self.runtime["_limit_json"](
                    directory / f"{self.runtime['LIMITED_PREFIX']}{name}"
                )
                if str(current.get("until") or "") > self.runtime["_utc_stamp"](now):
                    return None
            notice_id = os.urandom(16).hex()
            until = self.runtime["mark_limited"](
                directory,
                name,
                seconds,
                now=now,
                lease_id=lease_id,
                observed_at=observed_at,
                account=account,
                notice_id=notice_id,
            )
            current = self.runtime["_limit_json"](
                directory / f"{self.runtime['LIMITED_PREFIX']}{name}"
            )
            return current if until and current.get("notice_id") == notice_id else None

        def admit(name: str, owned: dict[str, Any] | None) -> None:
            with self.runtime["_limit_lock"](directory, name) as state:
                marker = directory / f"{self.runtime['LIMITED_PREFIX']}{name}"
                if lease_id is not None and (not (state / f"{lease_id}.json").exists()):
                    self.runtime["_write_limit_json"](
                        state / f"{lease_id}.json",
                        {
                            "account": account,
                            "observed_at": self.runtime["_utc_stamp"](
                                observed_at or now
                            ),
                        },
                    )
                if owned is not None and self.runtime["_limit_json"](marker) == owned:
                    marker.unlink(missing_ok=True)

        def retain(name: str) -> str | None:
            with self.runtime["_limit_lock"](directory, name) as state:
                current = self.runtime["_limit_json"](
                    directory / f"{self.runtime['LIMITED_PREFIX']}{name}"
                )
                if str(current.get("until") or "") >= self.runtime["_utc_stamp"](
                    now + timedelta(seconds=seconds)
                ):
                    if lease_id is not None and (
                        not (state / f"{lease_id}.json").exists()
                    ):
                        self.runtime["_write_limit_json"](
                            state / f"{lease_id}.json", current
                        )
                    return str(current["until"])
            return cast(
                "str | None",
                self.runtime["mark_limited"](
                    directory,
                    name,
                    seconds,
                    now=now,
                    lease_id=lease_id,
                    observed_at=observed_at,
                    account=account,
                ),
            )

        source_owned = bench(host)

        def apply(name: str) -> str | None:

            def record(
                decision: str, *, selected: str | None = None, until: str | None = None
            ) -> None:
                if decisions is not None:
                    decisions[name] = {
                        "decision": decision,
                        "limited_account": account,
                        "selected_account": selected,
                        "until": until,
                    }

            if name != host:
                with self.runtime["_limit_lock"](directory, name) as state:
                    consumed = (
                        lease_id is not None and (state / f"{lease_id}.json").exists()
                    )
                    passed = self._passed_at(state, account)
                if consumed or (passed is not None and (observed_at or now) <= passed):
                    record("consumed" if consumed else "stale")
                    return None
            pick = hosts.get(name)
            owned = source_owned if name == host else None
            if label is not None and pick is not None:
                rc = self._mark_pool_account(pick, label, seconds, capture)
                if name != host and rc not in (0, 4):
                    answered, _, selected = self._claude_room_verified(pick, capture)
                    if answered or not identities.intersection(selected):
                        record("unrelated-or-usable")
                        return None
                if rc in (0, 4):
                    if name != host:
                        owned = bench(name)
                    answered, selected_account, _ = self._claude_room_verified(
                        pick, capture
                    )
                    if answered:
                        admit(name, owned)
                        record("probe-answered", selected=selected_account)
                        return None
            elif (
                name == host
                and pick is not None
                and (account is not None)
                and account.startswith("native-")
            ):
                if self._mark_native_limited(pick, capture):
                    answered, selected_account, _ = self._claude_room_verified(
                        pick, capture
                    )
                    if answered:
                        admit(name, owned)
                        record("probe-answered", selected=selected_account)
                        return None
            elif name != host:
                if account is None or pick is None:
                    record("unrelated")
                    return None
                answered, _, selected = self._claude_room_verified(pick, capture)
                if answered or not identities.intersection(selected):
                    record("unrelated-or-usable")
                    return None
            until = retain(name)
            record("benched" if until else "consumed-or-recovered", until=until)
            return until

        names = [
            host,
            *[name for name in hosts if name != host and account is not None],
        ]
        with ThreadPoolExecutor(max_workers=len(names)) as pool:
            results = list(pool.map(apply, names))
        return results[0]

    def reprobe_limited(
        self,
        directory: Path,
        hosts: Sequence[Any],
        *,
        capture: Capture | None = None,
        now: datetime | None = None,
        interval_s: float = 900.0,
    ) -> dict[str, str]:
        """``limited_hosts`` after re-probing each standing marker that is due (OMN-20864): a host in ``hosts`` whose marker was last
        checked ``interval_s`` or more ago is asked ``claude -p`` once; an answer retains the recovery time and removes the marker,
        anything else stamps ``probed_at`` and keeps it. A limit notice marks the selected pool token and asks again through the
        lane's selector, so a good alternative admits the host. Recovery is scoped to the credential that answered. A newer marker
        is left alone, as is a marker for a host outside ``hosts``.
        The hosts are probed in parallel."""
        capture = capture or self.runtime["_capture"]
        now = now or datetime.now(UTC)
        limited = self.runtime["limited_hosts"](directory, now=now)
        by_name = {h.name: h for h in hosts}
        due: list[tuple[Any, dict[str, Any]]] = []
        for name in limited:
            host = by_name.get(name)
            marker = directory / f"{self.runtime['LIMITED_PREFIX']}{name}"
            body = self.runtime["_limit_json"](marker)
            if host is None or not body:
                continue
            checked = self.runtime["_marker_time"](
                body.get("probed_at")
            ) or self.runtime["_marker_time"](body.get("at"))
            if checked is None or (now - checked).total_seconds() >= interval_s:
                due.append((host, body))
        if not due:
            return cast("dict[str, str]", limited)

        def ask(item: tuple[Any, dict[str, Any]]) -> tuple[bool, str | None, set[str]]:
            return self._claude_room_verified(item[0], capture)

        with ThreadPoolExecutor(max_workers=len(due)) as pool:
            answers = list(pool.map(ask, due))
        for (host, body), (answered, account, _) in zip(due, answers, strict=True):
            with self.runtime["_limit_lock"](directory, host.name) as state:
                marker = directory / f"{self.runtime['LIMITED_PREFIX']}{host.name}"
                current = self.runtime["_limit_json"](marker)
                if answered:
                    recovery = self.runtime["_limit_json"](state / "passed.json")
                    previous = self._passed_at(state, account)
                    if previous is None or now > previous:
                        accounts = recovery.get("accounts")
                        accounts = dict(accounts) if isinstance(accounts, dict) else {}
                        if recovery.get("at"):
                            accounts.setdefault(
                                recovery.get("account") or "", recovery["at"]
                            )
                        accounts[account or ""] = self.runtime["_utc_stamp"](now)
                        self.runtime["_write_limit_json"](
                            state / "passed.json",
                            {
                                "at": self.runtime["_utc_stamp"](now),
                                "account": account,
                                "accounts": accounts,
                            },
                        )
                    if body.get("lease_id") is not None:
                        self.runtime["_write_limit_json"](
                            state / f"{body['lease_id']}.json", body
                        )
                    if current == body:
                        marker.unlink(missing_ok=True)
                elif current == body:
                    self.runtime["_write_limit_json"](
                        marker, {**body, "probed_at": self.runtime["_utc_stamp"](now)}
                    )
        return cast("dict[str, str]", self.runtime["limited_hosts"](directory, now=now))
