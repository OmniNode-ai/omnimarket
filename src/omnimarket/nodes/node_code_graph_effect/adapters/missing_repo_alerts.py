# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The alert for a declared repository the code graph could not read (OMN-26104).

A repository is named through the declared alert channel once, and again only when its reason changes
or it returns and goes missing again: the state file holds the reason each missing repository was alerted
for. A send that fails is not recorded, so the next run tries again, and it is named on the result as
``alert-failed``, never dropped.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path

from omnimarket.nodes.node_code_graph_effect.overlay import ModelCodeGraphAlert

#: ``(category, text)`` to ``(delivered, why)``; ``why`` is a token when it was not.
Send = Callable[[str, str], tuple[bool, str]]


class ShellAlertChannel:
    """Sends through the alert-channel shell library the overlay declares."""

    def __init__(
        self, declared: ModelCodeGraphAlert, environ: Mapping[str, str]
    ) -> None:
        self._declared = declared
        self._environ = environ

    def __call__(self, category: str, text: str) -> tuple[bool, str]:
        lib = self._environ.get(self._declared.lib_env, "").strip()
        if not lib or not os.access(lib, os.R_OK):
            return False, "library-unreadable"
        try:
            done = subprocess.run(
                [
                    self._declared.shell,
                    "-c",
                    'set -u; . "$1" && "$2" "$3" "$4"',
                    "_",
                    lib,
                    self._declared.function,
                    category,
                    text,
                ],
                capture_output=True,
                timeout=self._declared.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return False, "timeout"
        except OSError as exc:
            return False, f"os-error-{type(exc).__name__}"
        if done.returncode:
            return False, f"exit-{done.returncode}"
        return True, ""


class ChannelMissingRepoAlerts:
    """One alert per missing repository and reason, remembered in a state file."""

    def __init__(self, *, send: Send, category: str, state_path: Path | None) -> None:
        self._send = send
        self._category = category
        self._state_path = state_path

    def _load(self) -> dict[str, str]:
        assert self._state_path is not None
        try:
            loaded = json.loads(self._state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            # an unreadable state is never trusted as "alerted": alert again rather than stay silent
            return {}
        if not isinstance(loaded, dict):
            return {}
        return {str(k): str(v) for k, v in loaded.items()}

    def _store(self, state: Mapping[str, str]) -> None:
        assert self._state_path is not None
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        staged = self._state_path.with_name(
            f"{self._state_path.name}.{os.getpid()}.tmp"
        )
        staged.write_text(
            json.dumps(dict(state), sort_keys=True, indent=1), encoding="utf-8"
        )
        staged.replace(self._state_path)

    def announce(
        self, missing: Mapping[str, str], *, host: str, at: datetime
    ) -> tuple[str, ...]:
        if self._state_path is None:
            # no place to remember what was sent: an alert per run would flood, so none is sent and the gap is named
            return tuple(
                f"alert-failed:{repo}:state-path-unset" for repo in sorted(missing)
            )
        state = self._load()
        outcomes: list[str] = []
        for repo, reason in sorted(missing.items()):
            if state.get(repo) == reason:
                outcomes.append(f"already-alerted:{repo}")
                continue
            text = (
                f"code graph on {host}: repository {repo} cannot be read ({reason}) "
                f"at {at.strftime('%Y-%m-%dT%H:%M:%SZ')}; the graph is built from the repositories present"
            )
            delivered, why = self._send(self._category, text)
            if delivered:
                state[repo] = reason
                outcomes.append(f"alerted:{repo}")
            else:
                state.pop(repo, None)
                outcomes.append(f"alert-failed:{repo}:{why}")
        remembered = {repo: reason for repo, reason in state.items() if repo in missing}
        try:
            self._store(remembered)
        except OSError:
            outcomes = [
                outcome
                if not outcome.startswith("alerted:")
                else f"{outcome}:state-unwritten"
                for outcome in outcomes
            ]
        return tuple(outcomes)
