# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Validate and preserve setup profiles through the shared schema and lock."""

from __future__ import annotations

import fcntl
import json
import os
import stat
from pathlib import Path

from omnibase_core.cli.cli_user_config import user_config_path

from omnimarket.nodes.node_onex_setup_effect.handlers.handler_onex_setup import (
    _write_atomic,
)
from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_plugins import (
    HandlerSetupPlugins,
)
from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_workflow import (
    HandlerSetupWorkflow,
    _check_file,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_configuration import (
    ModelSetupConfigurationProfile,
    ModelSetupConfigurationRequest,
    ModelSetupConfigurationResult,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_plan import (
    ModelSetupPlan,
)

_MAX_PROFILE_BYTES = 1024 * 1024


class HandlerSetupConfiguration:
    """Profiles contain plan choices only; credentials and receipts stay owned."""

    def __init__(self, *, onex_home: Path | None = None) -> None:
        self._home = onex_home or user_config_path().parent

    @staticmethod
    def load_plan(source: Path) -> ModelSetupPlan:
        """Read a raw unattended plan or a versioned profile without side effects."""
        _check_file(source)
        fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_PROFILE_BYTES:
                raise ValueError("Configuration must be a bounded regular file")
            raw = stream.read(_MAX_PROFILE_BYTES + 1)
        if len(raw) > _MAX_PROFILE_BYTES:
            raise ValueError("Configuration exceeds the profile size limit")
        value = json.loads(raw)
        if isinstance(value, dict) and "schema_version" in value:
            return ModelSetupConfigurationProfile.model_validate(value).plan
        return ModelSetupPlan.model_validate(value)

    def _profile_path(self, name: str) -> Path:
        if self._home.is_symlink() or (self._home / "profiles").is_symlink():
            raise ValueError("Profile directories must not be symbolic links")
        return self._home / "profiles" / (name + ".json")

    def _validate(self, plan: ModelSetupPlan) -> ModelSetupConfigurationProfile:
        HandlerSetupPlugins(onex_home=self._home).handle(plan)
        return ModelSetupConfigurationProfile(plan=plan)

    def _import(
        self,
        request: ModelSetupConfigurationRequest,
        profile: ModelSetupConfigurationProfile,
    ) -> ModelSetupConfigurationResult:
        if request.name is None:
            raise ValueError("Import needs an explicit profile name")
        destination = self._profile_path(request.name)
        _check_file(destination)
        self._home.mkdir(parents=True, exist_ok=True)
        lock = self._home / "setup.lock"
        _check_file(lock)
        fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("Invalid setup lock")
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                _check_file(destination)
                before = destination.read_text() if destination.exists() else None
                content = profile.model_dump_json(indent=2) + "\n"
                changed = before != content
                if before is not None and changed and not request.replace:
                    raise ValueError("Existing profile needs explicit replacement")
                if changed:
                    _write_atomic(destination, content)
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)
        return ModelSetupConfigurationResult(
            status="ready",
            profile=profile,
            source=destination,
            changed=changed,
            message="Profile saved; apply it explicitly with onex setup --plan PATH.",
        )

    def handle(
        self, request: ModelSetupConfigurationRequest
    ) -> ModelSetupConfigurationResult:
        try:
            if request.operation == "list":
                directory = self._profile_path("probe").parent
                names: list[str] = []
                if directory.exists():
                    for candidate in sorted(directory.glob("*.json")):
                        ModelSetupConfigurationRequest(
                            operation="export", name=candidate.stem
                        )
                        self._validate(self.load_plan(candidate))
                        names.append(candidate.stem)
                return ModelSetupConfigurationResult(
                    status="ready", profiles=tuple(names)
                )
            source = request.source
            if source is None and request.name is not None:
                source = self._profile_path(request.name)
            if source is None:
                if request.operation == "import":
                    raise ValueError("Import needs a source file")
                plan = HandlerSetupWorkflow.saved_plan(self._home)
                source = self._home / "setup.json"
            else:
                plan = self.load_plan(source)
            if plan is None:
                return ModelSetupConfigurationResult(
                    status="unconfigured",
                    message="No saved setup plan. Run onex setup first.",
                )
            profile = self._validate(plan)
            if request.operation == "import":
                return self._import(request, profile)
            return ModelSetupConfigurationResult(
                status="ready", profile=profile, source=source
            )
        except (OSError, ValueError):
            # Validation errors can echo secret-bearing input. Never print them.
            return ModelSetupConfigurationResult(
                status="refused",
                message="Configuration refused. Check its schema, installed components, regular file paths and setup lock; use --replace only to replace a named profile. Secrets belong in the credential store.",
            )
