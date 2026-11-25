# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The readers behind the gate's protocols: a pull request text file, ledger rows as text, and a local patch id.

None of them reaches the network or names a path, host or address: each takes what it reads as an argument.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from omnimarket.nodes.node_landing_proof_gate_compute.models.model_proof_gate import (
    ModelLedgerNotice,
    ModelProofGatePolicy,
    ModelProofText,
)
from omnimarket.nodes.node_landing_proof_gate_compute.protocols.protocol_proof_gate import (
    ProtocolPatchIdReader,
)

ROW_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?Z) \| ([A-Z-]+) \|(.*)$"
)
FILE_DAY_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
NOTICE_TYPES = frozenset({"HOLD", "MSG", "RULING", "OPERATOR-CONSENT"})


def _cells(text: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for cell in text.split(" | "):
        m = re.match(r"^\s*([a-z_]+)=(.*)$", cell)
        if m:
            found.setdefault(m[1], m[2].strip())
    return found


class FileProofReader:
    """A pull request's text kept as one JSON file: ``body``, ``comments`` (strings) and ``files`` (paths)."""

    def __init__(self, path: str, load: Callable[[Path], str] = Path.read_text) -> None:
        self._path = path
        self._load = load

    def read(
        self, repo: str, number: int, base: str, head: str
    ) -> ModelProofText | None:
        try:
            raw = json.loads(self._load(Path(self._path)))
            comments = tuple(str(c) for c in raw["comments"])
            files = tuple(str(f) for f in raw["files"])
            return ModelProofText(
                body=str(raw.get("body") or ""), comments=comments, files=files
            )
        except (OSError, ValueError, KeyError, TypeError):
            return None


class LedgerFileReader:
    """Ledger rows as text: the live file and the archive files of the last ``ledger_days`` days beside it."""

    def __init__(
        self,
        policy: ModelProofGatePolicy,
        path: str,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._policy = policy
        self._path = path
        self._now = now or (lambda: datetime.now(UTC))

    def _files(self) -> list[Path] | None:
        live = Path(self._path)
        if not self._path or not live.is_file():
            return None
        horizon = (self._now() - timedelta(days=self._policy.ledger_days)).strftime(
            "%Y-%m-%d"
        )
        archive = [
            f
            for f in sorted((live.parent / "archive").glob("*.md"))
            if (m := FILE_DAY_RE.search(f.name)) and m[1] >= horizon
        ]
        return [*archive, live]

    @staticmethod
    def _rows(files: Iterable[Path], number: str) -> list[tuple[str, str, str]]:
        """(ts, row type, text) of every row that spells the number or is a RELEASE; a continuation joins its row."""
        out: list[tuple[str, str, str]] = []
        for f in files:
            row: list[str] | None = None
            for line in f.read_text(errors="replace").splitlines():
                m = ROW_RE.match(line)
                if m:
                    if row is not None and (row[1] == "RELEASE" or number in row[2]):
                        out.append((row[0], row[1], row[2]))
                    row = [m[1], m[2], m[3]]
                elif row is not None:
                    row[2] += "\n" + line
            if row is not None and (row[1] == "RELEASE" or number in row[2]):
                out.append((row[0], row[1], row[2]))
        return out

    def open_notices(
        self, repo: str, number: int
    ) -> tuple[ModelLedgerNotice, ...] | None:
        try:
            files = self._files()
            if files is None:
                return None
            rows = self._rows(files, str(number))
        except OSError:
            return None
        ref = re.compile(
            self._policy.pr_ref.format(repo=re.escape(repo), number=number)
        )
        phrase = re.compile(self._policy.do_not_land)
        now = self._now().strftime("%Y-%m-%dT%H:%M:%SZ")
        released = {
            c["re"]
            for _ts, t, text in rows
            if t == "RELEASE"
            and (c := _cells(text)).get("re")
            and (not c.get("pr") or ref.search(c["pr"]))
        }
        found: list[ModelLedgerNotice] = []
        for ts, rtype, text in rows:
            if rtype not in NOTICE_TYPES or not ref.search(text):
                continue
            c = _cells(text)
            row_id = c.get("id") or f"{ts}-{c.get('lane', '')}"
            until = c.get("until", "")
            if rtype == "HOLD":
                if row_id in released or (until and until < now):
                    continue
            elif not phrase.search(text) or row_id in released:
                continue
            found.append(ModelLedgerNotice(row_id=row_id, rtype=rtype, ts=ts))
        return tuple(found)


class GitPatchIdReader:
    """``git patch-id --stable`` of ``origin/<base>...<sha>`` in a clone: local objects only, no network.

    Every git call gets the time left before ``deadline_at`` (a ``time.monotonic`` value); with none left it answers None.
    """

    def __init__(
        self,
        clone: str,
        deadline_at: float,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._clone, self._deadline_at, self._now = clone, deadline_at, now

    def patch_id(self, repo: str, base: str, sha: str) -> str | None:
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        git = ["git", "-C", self._clone]
        try:
            left = self._deadline_at - self._now()
            if left <= 0:
                return None
            diff = subprocess.run(
                [*git, "diff", f"origin/{base}...{sha}"],
                capture_output=True,
                timeout=left,
                check=False,
                env=env,
            )
            left = self._deadline_at - self._now()
            if diff.returncode != 0 or not diff.stdout or left <= 0:
                return None
            pid = subprocess.run(
                [*git, "patch-id", "--stable"],
                input=diff.stdout,
                capture_output=True,
                timeout=left,
                check=False,
                env=env,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        words = pid.stdout.split()
        return words[0].decode() if pid.returncode == 0 and words else None


class DeadlinePatchIdReader:
    """Wraps a patch-id reader: once ``budget_s`` has passed since construction, every read answers None."""

    def __init__(
        self,
        inner: ProtocolPatchIdReader,
        budget_s: float,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._inner, self._budget_s, self._now = inner, budget_s, now
        self._started = now()

    def patch_id(self, repo: str, base: str, sha: str) -> str | None:
        if self._now() - self._started > self._budget_s:
            return None
        return self._inner.patch_id(repo, base, sha)
