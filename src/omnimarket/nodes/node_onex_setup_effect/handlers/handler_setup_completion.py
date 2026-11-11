# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Deliver an explicitly selected completion once through an installed OS tool."""

from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import SecretStr

from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_command import (
    HandlerSetupCommand,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_command_request import (
    ModelSetupCommandRequest,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_observation import (
    ModelSetupObservation,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_plan import (
    ModelSetupPlan,
)


class HandlerSetupCompletion:
    """The workflow lock serializes this once-only optional effect."""

    name = "completion-audio"

    def __init__(
        self,
        *,
        onex_home: Path,
        executable: Path | None = None,
        timeout_seconds: float = 10,
        name: str = "completion-audio",
        kind: Literal["audio", "notification"] = "audio",
    ) -> None:
        if not name or name in (".", "..") or Path(name).name != name:
            raise ValueError("Completion component identity must be a safe file name")
        self.name = name
        self._kind = kind
        self._home = onex_home
        self._marker = onex_home / f"{name}.json"
        self._tool = "say" if kind == "audio" else "osascript"
        selected = shutil.which(self._tool)
        self._executable = executable or (Path(selected) if selected else None)
        self._timeout = timeout_seconds

    def _state(self) -> str | None:
        try:
            fd = os.open(self._marker, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return None
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 1024:
                raise ValueError("Invalid completion state")
            data = json.loads(source.read(1025))
        if not isinstance(data, dict) or data.get("status") not in (
            "attempted",
            "completed",
            "failed",
        ):
            raise ValueError("Invalid completion state")
        return str(data["status"])

    def observe(self, plan: ModelSetupPlan) -> ModelSetupObservation:
        if self.name not in plan.components:
            return ModelSetupObservation(step=self.name, status="skipped")
        state = self._state()
        if state == "completed":
            return ModelSetupObservation(
                step=self.name,
                status="success",
                evidence={
                    "delivery": f"{self._kind} command completed once; reruns do not repeat"
                },
            )
        if state is not None:
            return ModelSetupObservation(
                step=self.name,
                status="action_required",
                fix=f"Completion {self._kind} was attempted but not confirmed. It will not repeat automatically; deselect {self.name} to continue silently.",
            )
        if self._executable is None or not os.access(self._executable, os.X_OK):
            return ModelSetupObservation(
                step=self.name,
                status="action_required",
                fix=f"The selected {self.name} component needs an installed {self._tool} executable. Deselect it to continue silently.",
            )
        return ModelSetupObservation(step=self.name, status="unavailable")

    def apply(self, plan: ModelSetupPlan) -> None:
        if self.name not in plan.components:
            raise ValueError("Completion requires explicit component consent")
        if plan.action == "verify":
            raise ValueError("Completion cannot run during verify")
        if self._state() is not None:
            return
        if self._executable is None or not os.access(self._executable, os.X_OK):
            raise ValueError("The completion executable is unavailable")
        self._home.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            fd = os.open(
                self._marker,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
            )
        except FileExistsError:
            return
        # Record the attempt before delivery: a crash must never surprise the
        # user with a repeated sound or notification during repair.
        with os.fdopen(fd, "w") as target:
            target.write('{"status":"attempted"}\n')
            target.flush()
            os.fsync(target.fileno())
        greeting = f"{plan.completion_name}, " if plan.completion_name else ""
        message = greeting + "Welcome to OmniNode. Your selected setup is complete."
        content = (
            "display notification "
            + json.dumps(message, ensure_ascii=False)
            + ' with title "OmniNode"'
            if self._kind == "notification"
            else message
        )
        result = HandlerSetupCommand().handle(
            ModelSetupCommandRequest(
                argv=(str(self._executable), "-")
                if self._kind == "notification"
                else (str(self._executable),),
                stdin=SecretStr(content),
                timeout_seconds=self._timeout,
            )
        )
        fd, filename = tempfile.mkstemp(prefix=f".{self.name}-", dir=self._home)
        temporary = Path(filename)
        try:
            with os.fdopen(fd, "w") as target:
                json.dump(
                    {"status": "completed" if result.returncode == 0 else "failed"},
                    target,
                )
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, self._marker)
        finally:
            temporary.unlink(missing_ok=True)

    def handle(self, plan: ModelSetupPlan) -> ModelSetupObservation:
        if plan.action != "verify":
            self.apply(plan)
        return self.observe(plan)
