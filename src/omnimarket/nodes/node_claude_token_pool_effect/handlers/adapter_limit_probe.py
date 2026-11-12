# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The pool's own headless probe of a limited label (OMN-23895), the port ``ProtocolLimitProbe`` in production.

One small Claude Code call under the label's stored token, placed by ``node_llm_delegation_call_effect``
(``runtime_claude_probe``, the same typed request and receipt the refill's credential probe uses). The token
travels in the call's environment only; the answer this returns names a verdict and a reset time, never text.
"""

from __future__ import annotations

import functools
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import SecretStr

from omnimarket.models.delegation.model_claude_credential_probe import (
    ModelClaudeCredentialProbeReceipt,
    ModelClaudeCredentialProbeRequest,
)
from omnimarket.nodes.node_claude_pool_limit_recheck_compute.models.model_limit_recheck import (
    ModelLimitProbeAnswer,
)
from omnimarket.nodes.node_claude_token_pool_effect.models.model_claude_token_rotation import (
    ModelLimitProbeSettings,
)

#: The module that places the call (the one the refill's credential probe runs).
PROBE_MODULE = "omnimarket.nodes.node_llm_delegation_call_effect.runtime_claude_probe"
_REDACT = re.compile(r"sk-ant-[A-Za-z0-9_-]+")
_MONTHS = {
    name: number
    for number, name in enumerate(
        (
            "jan",
            "feb",
            "mar",
            "apr",
            "may",
            "jun",
            "jul",
            "aug",
            "sep",
            "oct",
            "nov",
            "dec",
        ),
        start=1,
    )
}

#: (argv, stdin text, environment) -> (return code, stdout). Tests inject a fake; production runs the module.
ProbeRunner = Callable[[Sequence[str], str, dict[str, str]], tuple[int, str]]


#: Seconds the call may overrun the probe's own deadline before the runner gives up on the process.
_OVERRUN_SECONDS = 60.0


def _run_module(
    argv: Sequence[str],
    stdin: str,
    env: dict[str, str],
    *,
    timeout: float | None = None,
) -> tuple[int, str]:
    try:
        done = subprocess.run(
            list(argv),
            input=stdin,
            capture_output=True,
            text=True,
            env=env,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return 124, ""
    except OSError as exc:
        return 127, type(exc).__name__
    return done.returncode, done.stdout


def parse_reset(
    text: str, settings: ModelLimitProbeSettings, now: datetime
) -> datetime | None:
    """The reset instant a limit answer names, plus the settings' margin; ``None`` when it names none.

    The year is taken from ``now``; a date that would already be more than a day past is read as next year.
    """
    found = re.search(settings.reset_pattern, text, re.IGNORECASE)
    if found is None:
        return None
    groups = found.groupdict()
    month = _MONTHS.get((groups["month"] or "")[:3].lower())
    if month is None:
        return None
    hour = int(groups["hour"])
    ampm = (groups.get("ampm") or "").lower()
    if ampm == "pm" and hour < 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    try:
        reset = datetime(
            now.year,
            month,
            int(groups["day"]),
            hour,
            int(groups.get("minute") or 0),
            tzinfo=UTC,
        )
    except ValueError:
        return None
    if reset < now - timedelta(days=1):
        reset = reset.replace(year=reset.year + 1)
    return reset + timedelta(seconds=settings.reset_margin_seconds)


class HeadlessLimitProbe:
    """Probes one label's token with the typed credential probe and reads the answer against the settings."""

    def __init__(
        self,
        *,
        secret: Callable[[str], SecretStr | None],
        settings: ModelLimitProbeSettings,
        lane_overlay_yaml: Callable[[], str],
        run: ProbeRunner | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        python: str = sys.executable,
    ) -> None:
        self._secret = secret
        self._settings = settings
        self._lane_overlay_yaml = lane_overlay_yaml
        self._run: ProbeRunner = run or functools.partial(
            _run_module, timeout=settings.timeout_seconds + _OVERRUN_SECONDS
        )
        self._now = now
        self._python = python

    def probe(self, label: str) -> ModelLimitProbeAnswer:
        probed_at = self._now()
        token = self._secret(label)
        if token is None:
            return ModelLimitProbeAnswer(verdict="error", probed_at=probed_at)
        request = ModelClaudeCredentialProbeRequest(
            prompt=self._settings.prompt,
            model=self._settings.model,
            timeout_seconds=self._settings.timeout_seconds,
        )
        env = {
            key: value
            for key, value in os.environ.items()
            if key not in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")
        }
        env["CLAUDE_CODE_OAUTH_TOKEN"] = token.get_secret_value()
        with tempfile.TemporaryDirectory() as scratch:
            overlay = Path(scratch) / "lane_overlay.yaml"
            overlay.write_text(self._lane_overlay_yaml(), encoding="utf-8")
            rc, out = self._run(
                [self._python, "-m", PROBE_MODULE, "--overlay", str(overlay)],
                request.model_dump_json() + "\n",
                env,
            )
        return self._read(rc, _REDACT.sub("<redacted>", out), probed_at)

    def _read(self, rc: int, out: str, probed_at: datetime) -> ModelLimitProbeAnswer:
        receipt = _last_receipt(out)
        if receipt is None or receipt.exit_cause in ("launch_failed", "timeout"):
            return ModelLimitProbeAnswer(verdict="error", probed_at=probed_at)
        text = receipt.output
        if any(p.lower() in text.lower() for p in self._settings.limited_patterns):
            return ModelLimitProbeAnswer(
                verdict="limited",
                reset_at=parse_reset(text, self._settings, probed_at),
                probed_at=probed_at,
            )
        if receipt.exit_cause == "answered" and (receipt.exit_code or 0) == 0:
            return ModelLimitProbeAnswer(verdict="ok", probed_at=probed_at)
        return ModelLimitProbeAnswer(verdict="error", probed_at=probed_at)


def _last_receipt(text: str) -> ModelClaudeCredentialProbeReceipt | None:
    for line in reversed(text.splitlines()):
        if line.startswith("{"):
            try:
                return ModelClaudeCredentialProbeReceipt.model_validate_json(line)
            except ValueError:
                return None
    return None


__all__ = ["PROBE_MODULE", "HeadlessLimitProbe", "ProbeRunner", "parse_reset"]
