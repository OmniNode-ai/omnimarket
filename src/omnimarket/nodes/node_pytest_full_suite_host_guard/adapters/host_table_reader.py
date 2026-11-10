# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Reads the host tables and the per-login overlay for the pre-push guard (OMN-23367).

Moved out of the guard script unchanged in behaviour: absence is neutral, present untrusted state
refuses with a ValueError the guard turns into a refusal. The paths and names arrive as arguments.
"""

import os
import subprocess
from collections.abc import Mapping
from pathlib import Path

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayNotFoundError,
    load_structured_overlay,
)
from omnimarket.models.node_overlays.model_node_overlays import (
    ModelPytestHostGuardOverlay,
)
from omnimarket.nodes.node_pytest_full_suite_host_guard.handlers.handler_pytest_full_suite_host_guard import (
    HandlerPytestFullSuiteHostGuard,
)
from omnimarket.nodes.node_pytest_full_suite_host_guard.models.model_pytest_full_suite_host_guard import (
    ModelPytestFullSuiteHostGuardDecision,
    ModelPytestFullSuiteHostGuardRequest,
)

_GIT_LOCATION_ENV = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
)

Env = Mapping[str, str]


def login_overlay_text(env: Env, overlay_name: str, roots_env: str) -> str | None:
    """The first per-login overlay file on the roots; absence is neutral."""
    for root in filter(None, env.get(roots_env, "").split(os.pathsep)):
        path = Path(root) / overlay_name / "overlay.yaml"
        if path.is_file():
            try:
                return path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                raise ValueError(
                    f"per-login host overlay cannot be read: {exc}"
                ) from exc
    return None


def legacy_override_by_label(env: Env) -> dict[str, str]:
    """The node's overlay: per row label the env var that replaces that row's hostname (OMN-19184).

    Absence is neutral, like ``login_overlay_text``; a broken overlay refuses and never reads as an
    absent one.
    """
    try:
        overlay = load_structured_overlay(
            "node_pytest_full_suite_host_guard", ModelPytestHostGuardOverlay, env
        )
    except NodeOverlayNotFoundError:
        return {}
    return dict(overlay.legacy_override_by_label)


def shipped_table_text(repo_root: Path, host_table_rel: str) -> str:
    """The committed table, refusing a working copy that differs from HEAD."""
    committed = subprocess.run(
        ["git", "-C", str(repo_root), "show", f"HEAD:{host_table_rel}"],
        capture_output=True,
        timeout=15,
        check=False,
    )
    if committed.returncode != 0:
        raise ValueError(f"host table absent at HEAD ({host_table_rel})")
    working = repo_root / host_table_rel
    if working.exists() and working.read_bytes() != committed.stdout:
        raise ValueError("host table differs between the working tree and HEAD")
    return committed.stdout.decode("utf-8")


def workspace_config_root(
    env: Env, private_rel: str, resolver_script: Path
) -> Path | None:
    """Private config root from injection or the workspace owner beside OMNI_HOME."""
    configured = env.get("ONEX_WORKSPACE_CONFIG_ROOT", "").strip()
    if configured:
        return Path(configured)
    if not env.get("OMNI_HOME", "").strip():
        return None
    resolved = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; prepush_workspace_config_root',
            "resolver",
            str(resolver_script),
        ],
        env={**env, "PREPUSH_PRIVATE_TABLE_REL": private_rel},
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    root = resolved.stdout.strip()
    return Path(root) if resolved.returncode == 0 and root else None


def private_table_text(root: Path | None, private_rel: str) -> str | None:
    """Read private bytes; absence is neutral, present untrusted state refuses."""
    if root is None or not private_rel:
        return None
    path = root / private_rel
    try:
        working = path.read_bytes()
    except FileNotFoundError:
        if not path.is_symlink():
            return None
        raise ValueError(
            "private host table cannot be read: dangling symlink"
        ) from None
    except OSError as exc:
        raise ValueError(f"private host table cannot be read: {exc.strerror}") from exc
    git_env = {k: v for k, v in os.environ.items() if k not in _GIT_LOCATION_ENV}
    top = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
        env=git_env,
    )
    if top.returncode == 0 and Path(top.stdout.strip()).resolve() == root.resolve():
        committed = subprocess.run(
            ["git", "-C", str(root), "show", f"HEAD:{private_rel}"],
            capture_output=True,
            timeout=15,
            check=False,
            env=git_env,
        )
        if committed.returncode != 0:
            raise ValueError("private host table has no committed copy at HEAD")
        if working != committed.stdout:
            raise ValueError(
                "private host table differs between the working tree and HEAD"
            )
    return working.decode("utf-8")


def decide(
    table_text: str,
    env: Env,
    *,
    host: str,
    login: str,
    private_table_rel: str,
    host_table_rel: str,
    overlay_name: str,
    roots_env: str,
) -> ModelPytestFullSuiteHostGuardDecision:
    """The node's answer for a host and login, with the overlays read from ``env``."""
    return HandlerPytestFullSuiteHostGuard().handle(
        ModelPytestFullSuiteHostGuardRequest(
            table_text=table_text,
            login_overlay_text=login_overlay_text(env, overlay_name, roots_env),
            login=login,
            host=host,
            env_overrides={k: v for k, v in env.items() if k.startswith("PREPUSH_")},
            legacy_override_by_label=legacy_override_by_label(env),
            private_table_rel=private_table_rel,
            host_table_rel=host_table_rel,
        )
    )
