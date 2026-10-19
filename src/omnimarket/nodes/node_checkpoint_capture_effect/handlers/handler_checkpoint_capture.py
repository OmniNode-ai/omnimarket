# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Journal a written session checkpoint as a ``checkpoint`` content record (TA.20a).

The producer half of the ``checkpoint`` capture kind, owned by omnimarket and
called by the session checkpoint writer. The writer records Workflow launches
and TaskStop results in a JSON checkpoint; this module hands each written file
to the onex plugin's ``hook_content_capture.py --kind checkpoint``, which
journals it on the content topic the session-content projection folds. That
script owns the opt-out (``OMNICLAUDE_CONTENT_CAPTURE=0``), the drainer
refusal, the size policy and the topic, so this module only finds it, pipes it
the file's exact bytes (so the reassembled record hashes to the file's own
sha256) and says what it did in one line.

Which capture entry
-------------------
The capture loads omnimarket and PyYAML, so it runs under the onex plugin's
interpreter, not the writer's. ``capture_entries`` lists (interpreter, script)
pairs, first usable wins: ``OMNI_CONTENT_CAPTURE_PY`` (with
``OMNI_CONTENT_CAPTURE_PYTHON``) when set; otherwise each installed onex plugin
with its plugin-data venv, newest install first; then the omniclaude clone
under ``OMNI_HOME`` with its venv. A pair missing either half is not used.

Where it is loaded from
-----------------------
The writer loads this file BY PATH, not through the ``omnimarket.nodes``
package: a package import runs ``__init__`` modules along the way, a cost no
per-tool-call hook can pay. So the module level imports the standard library
only; the node's pydantic models are imported inside
:meth:`HandlerCheckpointCapture.handle`, which only the runtime calls. A test
pins that module-level import set.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from omnimarket.nodes.node_checkpoint_capture_effect.models import (
        ModelCheckpointCaptureRequest,
        ModelCheckpointCaptureResult,
    )

#: The onex plugin's content-capture entry, relative to a plugin root.
CAPTURE_REL = Path("hooks") / "lib" / "hook_content_capture.py"

#: Under the writer hook's own 10 s registration timeout, so a slow capture
#: never costs the checkpoint.
CAPTURE_TIMEOUT_S = 6.0

STATUS_OK = "ok"
STATUS_SKIPPED = "skipped"
STATUS_FAILED = "failed"


@dataclass(frozen=True)
class CaptureOutcome:
    """What one capture did; ``line`` is what the writer prints."""

    status: str
    detail: str
    python: str | None = None
    script: str | None = None
    returncode: int | None = None

    @property
    def warning(self) -> bool:
        return self.status == STATUS_FAILED

    @property
    def line(self) -> str:
        word = "FAILED" if self.warning else self.status
        return f"capture {word}: {self.detail}"


def _installed_onex_roots(plugins_dir: Path) -> list[tuple[str, Path]]:
    """(marketplace, install root) of each installed onex plugin, newest first."""
    try:
        manifest = json.loads(
            (plugins_dir / "installed_plugins.json").read_text(encoding="utf-8")
        )
        listed = manifest.get("plugins") or {}
    except (OSError, ValueError, AttributeError):
        return []
    if not isinstance(listed, dict):
        return []
    installs: list[tuple[str, str, Path]] = []
    for key, entries in listed.items():
        name, _, market = str(key).partition("@")
        if name != "onex" or not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, dict) and entry.get("installPath"):
                installs.append(
                    (
                        str(entry.get("lastUpdated") or ""),
                        market,
                        Path(str(entry["installPath"])),
                    )
                )
    installs.sort(key=lambda install: install[0], reverse=True)
    return [(market, root) for _, market, root in installs]


def capture_entries(
    env: Mapping[str, str],
    home: Path,
    *,
    script: str | None = None,
    python: str | None = None,
    registry_root: str | None = None,
) -> list[tuple[Path, Path]]:
    """(interpreter, capture script) pairs to try, first usable wins."""
    script = script or env.get("OMNI_CONTENT_CAPTURE_PY", "").strip()
    if script:
        python = python or env.get("OMNI_CONTENT_CAPTURE_PYTHON", "").strip()
        return [(Path(python or sys.executable), Path(script))]
    plugins_dir = home / ".claude" / "plugins"
    pairs = [
        (
            plugins_dir / "data" / f"onex-{market}" / ".venv" / "bin" / "python3",
            root / CAPTURE_REL,
        )
        for market, root in _installed_onex_roots(plugins_dir)
    ]
    registry = registry_root or env.get("OMNI_HOME", "").strip()
    if registry:
        clone = Path(registry) / "omniclaude"
        pairs.append(
            (
                clone / ".venv" / "bin" / "python3",
                clone / "plugins" / "onex" / CAPTURE_REL,
            )
        )
    return pairs


def capture_checkpoint(
    path: Path,
    session_id: str,
    *,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
    script: str | None = None,
    python: str | None = None,
    registry_root: str | None = None,
    timeout_s: float = CAPTURE_TIMEOUT_S,
) -> CaptureOutcome:
    """Pipe the written checkpoint to the capture; never raises."""
    environ = dict(os.environ if env is None else env)
    usable = [
        (interpreter, entry)
        for interpreter, entry in capture_entries(
            environ,
            home or Path.home(),
            script=script,
            python=python,
            registry_root=registry_root,
        )
        if entry.is_file() and os.access(interpreter, os.X_OK)
    ]
    if not usable:
        return CaptureOutcome(
            STATUS_SKIPPED,
            "no onex hook_content_capture.py with its interpreter was found",
        )
    interpreter, entry = usable[0]
    python_ran, script_ran = str(interpreter), str(entry)
    # The capture's own interpreter resolves its packages; the writer's
    # PYTHONPATH would shadow them.
    environ.pop("PYTHONPATH", None)
    try:
        done = subprocess.run(
            [
                str(interpreter),
                "-B",
                str(entry),
                "--kind",
                "checkpoint",
                "--session-id",
                session_id,
                "--correlation-id",
                session_id,
            ],
            input=path.read_bytes(),
            capture_output=True,
            timeout=timeout_s,
            env=environ,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        reason = " ".join(str(exc).splitlines())
        return CaptureOutcome(
            STATUS_FAILED,
            f"{type(exc).__name__}: {reason}",
            python=python_ran,
            script=script_ran,
        )
    said = [
        line.strip()
        for line in done.stderr.decode("utf-8", "replace").splitlines()
        if line.strip()
    ]
    return CaptureOutcome(
        STATUS_FAILED if done.returncode else STATUS_OK,
        said[-1] if said else "nothing journalled",
        python=python_ran,
        script=script_ran,
        returncode=done.returncode,
    )


class HandlerCheckpointCapture:
    """The node's handler: one written checkpoint in, what the capture did out."""

    def handle(
        self, request: ModelCheckpointCaptureRequest
    ) -> ModelCheckpointCaptureResult:
        from omnimarket.nodes.node_checkpoint_capture_effect.models import (
            EnumCheckpointCaptureStatus,
            ModelCheckpointCaptureResult,
        )

        outcome = capture_checkpoint(
            Path(request.checkpoint_path),
            request.session_id,
            home=Path(request.home) if request.home else None,
            script=request.capture_script,
            python=request.capture_python,
            registry_root=request.registry_root,
            timeout_s=request.timeout_s,
        )
        return ModelCheckpointCaptureResult(
            status=EnumCheckpointCaptureStatus(outcome.status),
            detail=outcome.detail,
            capture_script=outcome.script,
            capture_python=outcome.python,
            returncode=outcome.returncode,
        )


__all__ = [
    "CAPTURE_REL",
    "CAPTURE_TIMEOUT_S",
    "CaptureOutcome",
    "HandlerCheckpointCapture",
    "capture_checkpoint",
    "capture_entries",
]
