# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Store, deploy, probe and rotate a lab host's Claude token (OMN-19998).

A lab host's lanes authenticate with ``CLAUDE_CODE_OAUTH_TOKEN`` read from
``$HOME/.config/omni/claude-oauth-token``. Making a token (``claude setup-token``) is
the operator's act; everything after it is this handler's:

* ``set``: store the piped token under ``lab.claude_token.<host>.<account>``, write it
  to the host's token file (mode 0600, the old file kept as ``.prev``), ask Claude the
  same cheap question placement's limit re-probe asks, and on a clean answer drop the
  backup and the host's ``.limited-<host>`` marker. Anything else puts the previous
  token back on the host and in the store, and says so.
* ``add_spare``: keep a token as a spare account (``lab.claude_token.pool.<account>``,
  listed in ``lab.claude_token.pool``), deploying nothing.
* ``rotate_limited``: for a host placement has benched, mark its account limited until
  the marker's ``until``, then deploy and probe each spare account with headroom in
  turn; the first that answers becomes the host's account, the marker goes, and the
  rotation is returned as an event and appended to
  ``<placement_dir>/claude-token-rotations.jsonl``. With no spare it says so once per
  marker.

The token travels from stdin to the store and to the host's stdin only. No script,
argv, result, event, record or line carries it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from omnimarket.nodes.node_lab_claude_token_effect.models.model_lab_claude_token_request import (
    ModelLabClaudeTokenRequest,
    ModelLabTokenHost,
)
from omnimarket.nodes.node_lab_claude_token_effect.models.model_lab_claude_token_result import (
    ModelLabClaudeTokenResult,
    ModelLabClaudeTokenRotatedEvent,
)
from omnimarket.nodes.node_lab_claude_token_effect.protocols.protocol_lab_token_ports import (
    ProtocolLabHostShell,
    ProtocolLabTokenStore,
)

# Relative to the host's $HOME; the path placement's TOKEN_EXPORT and the remote engine read.
TOKEN_FILE = ".config/omni/claude-oauth-token"
LIMITED_PREFIX = ".limited-"
ROTATIONS_FILE = "claude-token-rotations.jsonl"
REF_ROOT = "lab.claude_token"
# Placement's usage-limit notices and its limit re-probe's question and bound.
LIMIT_MARKERS = (
    "you've hit your",
    "you have hit your",
    "usage limit reached",
    "limit reached",
    "out of extra usage",
)
PROBE_TIMEOUT_S = 120.0
SHELL_TIMEOUT_S = 30.0
# A spare that answered a limit (or nothing) is skipped for this long.
SPARE_BACKOFF_S = 3600.0


def account_ref(host: str, account: str) -> str:
    return f"{REF_ROOT}.{host}.{account}"


def active_ref(host: str) -> str:
    return f"{REF_ROOT}.{host}.active"


def no_spare_ref(host: str) -> str:
    return f"{REF_ROOT}.{host}.no_spare_reported"


def pool_ref(account: str) -> str:
    return f"{REF_ROOT}.pool.{account}"


def pool_index_ref() -> str:
    return f"{REF_ROOT}.pool"


def limited_ref(account: str) -> str:
    return f"{REF_ROOT}.limited.{account}"


_HEAD = f'set -u\numask 077\nT="$HOME/{TOKEN_FILE}"\n'
DEPLOY_SCRIPT = (
    "# op=deploy\n" + _HEAD + 'mkdir -p "$(dirname "$T")" || exit 73\n'
    'if [ -f "$T" ]; then cp -p "$T" "$T.prev" || exit 73; else rm -f "$T.prev"; fi\n'
    'cat > "$T.new" || exit 74\n'
    'if [ ! -s "$T.new" ]; then rm -f "$T.new"; exit 65; fi\n'
    'chmod 600 "$T.new" && mv -f "$T.new" "$T" || exit 74\n'
)
RESTORE_SCRIPT = (
    "# op=restore\n" + _HEAD + 'rm -f "$T.new"\n'
    'if [ -f "$T.prev" ]; then mv -f "$T.prev" "$T"; else rm -f "$T"; fi\n'
)
COMMIT_SCRIPT = "# op=commit\n" + _HEAD + 'rm -f "$T.prev"\n'
# Placement's limit re-probe, with the deployed token exported unconditionally: the
# question is whether this token answers, not whether the host has some login.
PROBE_SCRIPT = (
    "# op=probe\n" + _HEAD + 'PATH="$HOME/.local/bin:$PATH"; export PATH\n'
    "command -v claude >/dev/null 2>&1 || exit 127\n"
    '[ -r "$T" ] || exit 66\n'
    "unset ANTHROPIC_API_KEY\n"
    'CLAUDE_CODE_OAUTH_TOKEN="$(cat "$T")"; export CLAUDE_CODE_OAUTH_TOKEN\n'
    'B=; if command -v timeout >/dev/null 2>&1; then B="timeout '
    + str(int(PROBE_TIMEOUT_S) - 30)
    + '"; fi\n'
    '$B claude -p "Reply with the single word ok" --model sonnet --output-format text 2>&1\n'
)


def probe_answered(returncode: int, stdout: str) -> bool:
    """Placement's judge: exit 0, the word ``ok``, and no usage-limit notice."""
    text = stdout.lower()
    return (
        returncode == 0
        and re.search(r"\bok\b", text) is not None
        and not any(m in text for m in LIMIT_MARKERS)
    )


def _stamp(when: datetime) -> str:
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _utc_now() -> datetime:
    return datetime.now(UTC)


class HandlerLabClaudeToken:
    """Runs one ``ModelLabClaudeTokenRequest`` against a store and a host shell."""

    def __init__(
        self,
        *,
        store: ProtocolLabTokenStore,
        shell: ProtocolLabHostShell,
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._store = store
        self._shell = shell
        self._now = now

    def handle(self, request: ModelLabClaudeTokenRequest) -> ModelLabClaudeTokenResult:
        if request.operation == "add_spare":
            return self._add_spare(request)
        if request.operation == "set":
            return self._set(request)
        return self._rotate(request)

    # --- set ---------------------------------------------------------------------------------------

    def _set(self, request: ModelLabClaudeTokenRequest) -> ModelLabClaudeTokenResult:
        host, account = request.host, request.account
        assert host is not None
        assert account is not None
        ref = account_ref(host.name, account)
        marker = Path(request.placement_dir) / f"{LIMITED_PREFIX}{host.name}"
        if request.dry_run:
            return ModelLabClaudeTokenResult(
                operation="set",
                outcome="planned",
                host=host.name,
                account=account,
                secret_ref=ref,
                lines=(
                    f"dry run: store the stdin token at {ref}",
                    f"dry run: write ~/{TOKEN_FILE} mode 0600 on {self._where(host)}, previous kept as .prev",
                    "dry run: probe claude -p (sonnet) with that token; judge exit 0, 'ok', no limit notice",
                    f"dry run: on an answer remove {marker} ({'present' if marker.exists() else 'absent'}), "
                    f"set {active_ref(host.name)} to {account}",
                    "dry run: on no answer restore the previous token on the host and in the store",
                ),
            )
        assert request.token is not None
        token = request.token.get_secret_value().strip()
        previous = self._store.get(ref)
        self._store.put(ref, token)
        answered = self._try(host, token)
        if not answered:
            if previous is None:
                self._store.delete(ref)
            else:
                self._store.put(ref, previous)
            return ModelLabClaudeTokenResult(
                operation="set",
                outcome="restored",
                host=host.name,
                account=account,
                secret_ref=ref,
                probe_answered=False,
                lines=(
                    f"{host.name}: the token for {account} did not answer the probe; "
                    f"the previous token is restored on the host and at {ref}",
                ),
            )
        self._store.put(active_ref(host.name), account)
        cleared = self._clear_marker(marker)
        self._store.delete(no_spare_ref(host.name))
        return ModelLabClaudeTokenResult(
            operation="set",
            outcome="deployed",
            host=host.name,
            account=account,
            secret_ref=ref,
            probe_answered=True,
            marker_cleared=cleared,
            lines=(
                f"{host.name}: {account} answered the probe; stored at {ref}; "
                + (f"removed {marker.name}" if cleared else "no limit marker stood"),
            ),
        )

    # --- add_spare ---------------------------------------------------------------------------------

    def _add_spare(
        self, request: ModelLabClaudeTokenRequest
    ) -> ModelLabClaudeTokenResult:
        account = request.account
        assert account is not None
        ref = pool_ref(account)
        if request.dry_run:
            return ModelLabClaudeTokenResult(
                operation="add_spare",
                outcome="planned",
                account=account,
                secret_ref=ref,
                lines=(
                    f"dry run: store the stdin token at {ref} and list {account} in {pool_index_ref()}",
                ),
            )
        assert request.token is not None
        self._store.put(ref, request.token.get_secret_value().strip())
        pool = self._pool()
        if account not in pool:
            self._store.put(pool_index_ref(), ",".join([*pool, account]))
        return ModelLabClaudeTokenResult(
            operation="add_spare",
            outcome="spare_added",
            account=account,
            secret_ref=ref,
            lines=(f"spare {account} stored at {ref}; pool: {','.join(self._pool())}",),
        )

    # --- rotate_limited ----------------------------------------------------------------------------

    def _rotate(self, request: ModelLabClaudeTokenRequest) -> ModelLabClaudeTokenResult:
        host = request.host
        assert host is not None
        marker = Path(request.placement_dir) / f"{LIMITED_PREFIX}{host.name}"
        until = self._marker_until(marker)
        if until is None:
            return ModelLabClaudeTokenResult(
                operation="rotate_limited",
                outcome="not_limited",
                host=host.name,
                lines=(f"{host.name}: no standing {marker.name}; nothing to rotate",),
            )
        current = self._store.get(active_ref(host.name))
        candidates = self._candidates(current)
        if request.dry_run:
            return ModelLabClaudeTokenResult(
                operation="rotate_limited",
                outcome="planned",
                host=host.name,
                account=current,
                lines=(
                    f"dry run: {host.name} is limited until {until}; its account {current or '(unrecorded)'} "
                    f"would be marked at {limited_ref(current) if current else '(none)'}",
                    *(
                        f"dry run: candidate {a}: deploy {pool_ref(a)} to ~/{TOKEN_FILE} on {self._where(host)} "
                        f"and probe"
                        for a in candidates
                    ),
                    *(
                        ()
                        if candidates
                        else (f"dry run: no spare with headroom for {host.name}",)
                    ),
                ),
            )
        if current:
            self._store.put(limited_ref(current), until)
        for account in candidates:
            token = self._store.get(pool_ref(account))
            if not token:
                continue
            if not self._try(host, token):
                self._store.put(
                    limited_ref(account),
                    _stamp(self._now() + timedelta(seconds=SPARE_BACKOFF_S)),
                )
                continue
            ref = account_ref(host.name, account)
            self._store.put(ref, token)
            self._store.put(active_ref(host.name), account)
            self._store.delete(no_spare_ref(host.name))
            cleared = self._clear_marker(marker)
            event = ModelLabClaudeTokenRotatedEvent(
                host=host.name,
                from_account=current,
                to_account=account,
                to_secret_ref=ref,
                limited_until=until,
                rotated_at=self._now(),
            )
            self._record(Path(request.placement_dir), event)
            return ModelLabClaudeTokenResult(
                operation="rotate_limited",
                outcome="rotated",
                host=host.name,
                account=account,
                secret_ref=ref,
                probe_answered=True,
                marker_cleared=cleared,
                events=(event,),
                lines=(
                    f"{host.name}: rotated from {current or '(unrecorded)'} to {account} ({ref}); "
                    f"removed {marker.name}",
                ),
            )
        already = self._store.get(no_spare_ref(host.name)) == until
        self._store.put(no_spare_ref(host.name), until)
        return ModelLabClaudeTokenResult(
            operation="rotate_limited",
            outcome="no_spare",
            host=host.name,
            account=current,
            reported=not already,
            lines=()
            if already
            else (
                f"{host.name}: limited until {until} and no spare account with headroom in {pool_index_ref()}; "
                "add one with `onex lab claude-token add-spare`",
            ),
        )

    # --- shared ------------------------------------------------------------------------------------

    def _try(self, host: ModelLabTokenHost, token: str) -> bool:
        """Deploy ``token`` and probe it; keep it on an answer, restore the previous file otherwise."""
        deployed = self._shell.run(
            host, DEPLOY_SCRIPT, stdin=token, timeout_s=SHELL_TIMEOUT_S
        )
        if deployed.returncode != 0:
            self._shell.run(host, RESTORE_SCRIPT, timeout_s=SHELL_TIMEOUT_S)
            return False
        probe = self._shell.run(host, PROBE_SCRIPT, timeout_s=PROBE_TIMEOUT_S)
        if probe_answered(probe.returncode, probe.stdout):
            self._shell.run(host, COMMIT_SCRIPT, timeout_s=SHELL_TIMEOUT_S)
            return True
        self._shell.run(host, RESTORE_SCRIPT, timeout_s=SHELL_TIMEOUT_S)
        return False

    def _pool(self) -> list[str]:
        return [a for a in (self._store.get(pool_index_ref()) or "").split(",") if a]

    def _candidates(self, current: str | None) -> list[str]:
        stamp = _stamp(self._now())
        return [
            a
            for a in self._pool()
            if a != current and (self._store.get(limited_ref(a)) or "") <= stamp
        ]

    def _marker_until(self, marker: Path) -> str | None:
        try:
            until = str(
                json.loads(marker.read_text(encoding="utf-8")).get("until") or ""
            )
        except (OSError, ValueError, AttributeError):
            return None
        return until if until > _stamp(self._now()) else None

    @staticmethod
    def _clear_marker(marker: Path) -> bool:
        existed = marker.exists()
        marker.unlink(missing_ok=True)
        return existed

    @staticmethod
    def _record(directory: Path, event: ModelLabClaudeTokenRotatedEvent) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / ROTATIONS_FILE).open("a", encoding="utf-8") as out:
            out.write(event.model_dump_json() + "\n")

    @staticmethod
    def _where(host: ModelLabTokenHost) -> str:
        return (
            f"{host.name} (local)"
            if host.local
            else f"{host.name} via ssh {host.target}"
        )
