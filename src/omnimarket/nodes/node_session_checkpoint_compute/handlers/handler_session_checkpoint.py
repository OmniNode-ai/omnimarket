# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Session checkpoints bound to the session's principals.

The session is bound first, through the team identity declaration (``omnimarket.session_scope``):
an operator session reads every checkpoint as before; a member session reads, resumes from and
inherits from only checkpoints carrying one of its own principals, and every checkpoint it writes
carries its principal.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from omnimarket.session_scope import bind_session_scope

from ..models.model_session_checkpoint import (
    ModelSessionCheckpointRequest,
    ModelSessionCheckpointResult,
)
from . import checkpoint as ckpt
from . import hook


class HandlerSessionCheckpoint:
    def handle(
        self, request: ModelSessionCheckpointRequest
    ) -> ModelSessionCheckpointResult:
        scope = bind_session_scope(request.identity_claims, request.overlay_roots)
        base: dict[str, Any] = {"operation": request.operation, "scope": scope}
        directory = Path(request.directory)
        now = ckpt.parse_stamp(request.now) if request.now else hook.utc_now()
        try:
            if request.operation == "write":
                spec = request.write
                if spec is None:
                    raise ckpt.InputError("operation write needs a write spec")
                start: dict[str, Any] | None = None
                if spec.from_file:
                    try:
                        start = json.loads(
                            Path(spec.from_file).read_text(encoding="utf-8")
                        )
                    except OSError as exc:
                        raise ckpt.InputError(f"cannot read from_file: {exc}") from exc
                    except json.JSONDecodeError as exc:
                        raise ckpt.RefusedError(
                            [f"from_file is not JSON: {exc}"]
                        ) from exc
                data = ckpt.build(
                    lane=spec.lane,
                    now=ckpt.stamp(now),
                    scope=scope,
                    base=start,
                    ticket=spec.ticket,
                    session_id=spec.session_id,
                    workflows=spec.workflows,
                    decisions=spec.decisions,
                    deadlines=spec.deadlines,
                    ticks=spec.ticks,
                    notes=spec.notes,
                )
                path = ckpt.write(data, directory)
                return ModelSessionCheckpointResult(
                    **base, status="ok", path=str(path), message=ckpt.summary(data)
                )
            if request.operation == "validate":
                if not request.path:
                    raise ckpt.InputError("operation validate needs a path")
                ckpt.load(Path(request.path))
                return ModelSessionCheckpointResult(
                    **base, status="ok", path=request.path, message="VALID"
                )
            if request.operation == "read":
                if request.path:
                    path = Path(request.path)
                    data = ckpt.load(path)
                    if not ckpt.owned(scope, data):
                        raise ckpt.RefusedError(
                            [
                                "member session: the checkpoint was written by another "
                                "principal (or names none); nothing in it is resumed"
                            ]
                        )
                else:
                    found = ckpt.newest(directory, scope)
                    if found is None:
                        prose = ckpt.newest_prose_handoff(directory, scope)
                        return ModelSessionCheckpointResult(
                            **base,
                            status="absent",
                            prose_handoff=str(prose) if prose else None,
                            message=f"NO-CHECKPOINT dir={directory}",
                        )
                    path = found
                    data = ckpt.load(path)
                return ModelSessionCheckpointResult(
                    **base,
                    status="ok",
                    path=str(path),
                    lines=tuple(ckpt.read_lines(data, path)),
                )
            spec_hook = request.hook
            if spec_hook is None:
                raise ckpt.InputError("operation record needs a hook spec")
            if spec_hook.landing_worker or spec_hook.hook_mode == "off":
                return ModelSessionCheckpointResult(
                    **base, status="skipped", message="hook off for this session"
                )
            if not hook.applies(
                spec_hook.payload,
                entrypoint=spec_hook.entrypoint,
                hook_mode=spec_hook.hook_mode,
                omni_home=spec_hook.omni_home,
            ):
                return ModelSessionCheckpointResult(
                    **base,
                    status="skipped",
                    message="not an interactive session of this home",
                )
            try:
                path, action, _ = hook.record(
                    spec_hook.payload,
                    directory=directory,
                    scope=scope,
                    now=now,
                    sessions_dir=Path(spec_hook.sessions_dir)
                    if spec_hook.sessions_dir
                    else None,
                )
            except hook.SkippedError as exc:
                return ModelSessionCheckpointResult(
                    **base, status="skipped", message=str(exc)
                )
            return ModelSessionCheckpointResult(
                **base,
                status="ok",
                path=str(path),
                message=f"checkpoint: {action} -> {path}",
            )
        except ckpt.RefusedError as exc:
            return ModelSessionCheckpointResult(
                **base, status="refused", findings=tuple(exc.findings)
            )
        except ckpt.InputError as exc:
            return ModelSessionCheckpointResult(
                **base, status="error", message=str(exc)
            )
