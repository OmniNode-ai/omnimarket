# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Reconcile setup effects from live observations, checkpointing each result.

Adapters own provisioning and evidence. A saved checkpoint is a recovery record,
never proof that the desired effect still exists. This handler is independent of
the legacy CLI until its component integrations are ready.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import shlex
import stat
import tempfile
from collections.abc import Callable
from pathlib import Path

from omnibase_core.cli.cli_user_config import user_config_path
from pydantic import SecretStr, ValidationError

from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_plugins import (
    HandlerSetupPlugins,
    SetupPluginConfigurationError,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_onex_setup import (
    ModelOnexSetupReceipt,
    ModelOnexSetupRequest,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_journal import (
    ModelSetupJournal,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_legacy_hints import (
    ModelSetupLegacyHints,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_observation import (
    ModelSetupObservation,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_plan import (
    ModelSetupPlan,
)
from omnimarket.nodes.node_onex_setup_effect.protocols.protocol_setup_step import (
    ProtocolSetupStep,
)


def _check_file(path: Path) -> None:
    """Refuse indirect or non-regular state without opening a FIFO/device."""
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return
    if not stat.S_ISREG(mode):
        raise ValueError("Invalid setup state: expected a regular file")


def _previous_state(state_path: Path) -> tuple[bool, ModelSetupJournal | None]:
    """Validate either state format; return whether a migration is needed."""
    _check_file(state_path)
    try:
        fd = os.open(state_path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return False, None
    with os.fdopen(fd, "rb") as source:
        raw = source.read()
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError
        if "schema_version" in data:
            return False, ModelSetupJournal.model_validate_json(raw, strict=True)
        if set(data) != {"workspace", "install_path"}:
            raise ValueError
        if not isinstance(data["workspace"], str) or not data["workspace"].strip():
            raise ValueError
        if data["install_path"] not in ("local", "docker"):
            raise ValueError
        return True, None
    except (ValueError, UnicodeDecodeError, ValidationError):
        # Validation errors can contain secret-bearing input. Never echo them.
        raise ValueError(
            "Invalid setup state: repair the state file before retrying"
        ) from None


def _checkpoint(state_path: Path, journal: ModelSetupJournal) -> None:
    """Replace only complete JSON; an interrupted write retains the old file."""
    _check_file(state_path)
    fd, name = tempfile.mkstemp(prefix=".setup-", suffix=".tmp", dir=state_path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as target:
            target.write(journal.model_dump_json() + "\n")
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, state_path)
        directory = os.open(state_path.parent, os.O_RDONLY)
        try:
            try:
                os.fsync(directory)
            except OSError as exc:
                if exc.errno not in (errno.EINVAL, errno.ENOTSUP):
                    raise
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


class HandlerSetupWorkflow:
    """Run a supplied, ordered set of owned setup adapters under one lock."""

    @staticmethod
    def supported_components() -> frozenset[str]:
        """Selections with an actual adapter in the installed default workflow."""
        return HandlerSetupPlugins().components()

    @staticmethod
    def saved_plan(onex_home: Path) -> ModelSetupPlan | None:
        """Read the authoritative versioned choices without starting an effect."""
        _, previous = _previous_state(onex_home / "setup.json")
        return previous.plan if previous is not None else None

    def __init__(
        self,
        *,
        onex_home: Path | None = None,
        steps: tuple[ProtocolSetupStep, ...] | None = None,
        plugins: HandlerSetupPlugins | None = None,
        legacy_onboarding: Path | None = None,
        read_legacy: Callable[[Path], ModelSetupLegacyHints | None] | None = None,
        credentials: dict[str, SecretStr] | None = None,
        model_step: Callable[[ModelOnexSetupRequest], ModelOnexSetupReceipt]
        | None = None,
    ) -> None:
        onex_home = onex_home or user_config_path().parent
        self._default_steps = steps is None
        if plugins is not None and steps is not None:
            raise ValueError("Choose plugin composition or explicit steps, not both")
        self._plugins = plugins or (
            HandlerSetupPlugins(onex_home=onex_home) if steps is None else None
        )
        self._context: dict[str, object] = {
            "onex_home": onex_home,
            "credentials": credentials,
            "model_step": model_step,
        }
        if steps is None:
            from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_legacy_import import (
                HandlerSetupLegacyImport,
            )

            steps = ()
            if legacy_onboarding is None:
                legacy_onboarding = Path.home() / ".omninode" / "onboarding"
                read_legacy = HandlerSetupLegacyImport().handle
        names = [step.name for step in steps]
        if (not self._default_steps and not names) or any(
            not name.strip() for name in names
        ):
            raise ValueError("Setup requires named steps")
        if len(set(names)) != len(names):
            raise ValueError("Setup requires unique step names")
        self._home = onex_home.expanduser().resolve()
        self._steps = steps
        if legacy_onboarding is not None and read_legacy is None:
            raise ValueError("Setup requires its legacy import adapter")
        self._legacy = legacy_onboarding
        self._read_legacy = read_legacy

    @staticmethod
    def _failure(step: str, *, changed: bool) -> ModelSetupObservation:
        return ModelSetupObservation(
            step=step,
            status="failure",
            changed=changed,
            fix="Check the component, then run onex setup --action repair.",
        )

    def _observe(
        self, step: ProtocolSetupStep, plan: ModelSetupPlan
    ) -> ModelSetupObservation:
        try:
            observation = step.observe(plan)
            if observation.step != step.name:
                return self._failure(step.name, changed=False)
            return observation.model_copy(update={"changed": False})
        except Exception:
            return self._failure(step.name, changed=False)

    def _run(
        self,
        plan: ModelSetupPlan,
        *,
        imported: bool,
        persist: bool,
        hints: ModelSetupLegacyHints | None = None,
        previous: ModelSetupJournal | None = None,
    ) -> ModelSetupJournal:
        desired = plan.model_dump(mode="json", exclude={"action"})
        digest = hashlib.sha256(
            json.dumps(desired, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        observations: list[ModelSetupObservation] = []
        earlier = (
            {item.step: item for item in previous.observations} if previous else {}
        )
        checkpoint_needed = previous is None or previous.desired_digest != digest
        for step in self._steps:
            observation = self._observe(step, plan)
            if (
                persist
                and observation.status == "unavailable"
                and step.name != "preflight"
            ):
                try:
                    applied = step.apply(plan)
                    if applied is not None and applied.status not in (
                        "success",
                        "skipped",
                    ):
                        observation = (
                            applied
                            if applied.step == step.name
                            else self._failure(step.name, changed=True)
                        )
                    else:
                        observation = self._observe(step, plan).model_copy(
                            update={"changed": True}
                        )
                except Exception:
                    # An effect may fail after a partial mutation; preserve that fact.
                    observation = self._failure(step.name, changed=True)
            observations.append(observation)
            if observation.status not in ("success", "skipped"):
                observation = observation.model_copy(
                    update={
                        "retry": shlex.join(
                            [
                                "onex",
                                "setup",
                                "--action",
                                "repair",
                                "--workspace",
                                str(plan.workspace),
                                "--yes",
                                "--no-interactive",
                            ]
                        )
                    }
                )
                observations[-1] = observation
            old = earlier.get(step.name)
            # A successful read-only rerun must not replace complete state with
            # a series of identical partial journals. Retain the last mutation's
            # record while still re-observing every selected effect now.
            if (
                observation.changed
                or old is None
                or old.model_dump(exclude={"changed", "retry"})
                != observation.model_dump(exclude={"changed", "retry"})
            ):
                checkpoint_needed = True
            journal = ModelSetupJournal(
                workspace=plan.workspace,
                install_path="docker" if "docker" in plan.components else "local",
                desired_digest=digest,
                observations=tuple(observations),
                imported_legacy=imported,
                plan=plan,
                legacy_hints=hints,
            )
            if persist and checkpoint_needed:
                _checkpoint(self._home / "setup.json", journal)
            if observation.status in ("failure", "action_required"):
                break
            if persist and observation.status not in ("success", "skipped"):
                break
        return journal

    def handle(self, plan: ModelSetupPlan) -> ModelSetupJournal:
        """Verify without writes, or reconcile with atomic per-step checkpoints."""
        if self._plugins is not None:
            try:
                self._steps = self._plugins.steps(
                    plan.components, context=self._context
                )
            except SetupPluginConfigurationError:
                return ModelSetupJournal(
                    workspace=plan.workspace,
                    install_path="docker" if "docker" in plan.components else "local",
                    desired_digest=hashlib.sha256(
                        plan.model_dump_json().encode()
                    ).hexdigest(),
                    plan=plan,
                    observations=(
                        ModelSetupObservation(
                            step="user_config",
                            status="failure",
                            fix="Review config.yaml and its setup_plugins section; configuration was preserved.",
                            retry=shlex.join(
                                [
                                    "onex",
                                    "setup",
                                    "--workspace",
                                    str(plan.workspace),
                                    "--action",
                                    "repair",
                                ]
                            ),
                        ),
                    ),
                )
        state = self._home / "setup.json"
        lock = self._home / "setup.lock"
        _check_file(state)
        _check_file(lock)
        if plan.action == "verify":
            _, previous = _previous_state(state)
            return self._run(
                plan,
                imported=False,
                persist=False,
                hints=previous.legacy_hints if previous is not None else None,
                previous=previous,
            )

        self._home.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError("Invalid setup state: lock must be a regular file")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError(
                    "A concurrent setup run holds the setup lock"
                ) from None
            imported, previous = _previous_state(state)
            hints = previous.legacy_hints if previous is not None else None
            if (
                previous is None
                and self._legacy is not None
                and self._read_legacy is not None
            ):
                hints = self._read_legacy(self._legacy)
                imported = imported or hints is not None
            return self._run(
                plan, imported=imported, persist=True, hints=hints, previous=previous
            )
        finally:
            # Closing also releases the lock after exceptions and process exit.
            os.close(fd)
