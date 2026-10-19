# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Configure hardlinks without overwriting operator settings or login profiles."""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import tempfile
import tomllib
from collections.abc import Callable, Mapping
from pathlib import Path

from ..models import (
    ModelLabUvCacheColocateRequest,
    ModelLabUvCacheColocateResult,
)

MANAGED_COMMENT = (
    "# Managed by onex lab disk growth (OMN-17427): hardlinks need the uv cache "
    "on the worktrees' filesystem."
)


def default_uv_config() -> Path:
    return (
        Path(os.environ.get("XDG_CONFIG_HOME") or "~/.config").expanduser()
        / "uv"
        / "uv.toml"
    )


def _login_env() -> Mapping[str, str]:
    names = ("UV_CACHE_DIR", "UV_LINK_MODE")
    try:
        proc = subprocess.run(
            [
                "bash",
                "-lc",
                'printf "%s\\n%s\\n" "${UV_CACHE_DIR:-}" "${UV_LINK_MODE:-}"',
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        values = proc.stdout.splitlines() if proc.returncode == 0 else []
    except (OSError, subprocess.TimeoutExpired):
        values = []
    return {
        name: (values[i] if i < len(values) and values[i] else os.environ.get(name, ""))
        for i, name in enumerate(names)
    }


def _overrides(env: Mapping[str, str], cache_dir: Path) -> tuple[str, ...]:
    overrides: list[str] = []
    cache = env.get("UV_CACHE_DIR", "")
    if (
        cache
        and cache != str(cache_dir)
        and Path(cache).expanduser().resolve() != cache_dir.resolve()
    ):
        overrides.append(f"UV_CACHE_DIR={cache}")
    mode = env.get("UV_LINK_MODE", "")
    if mode and mode != "hardlink":
        overrides.append(f"UV_LINK_MODE={mode}")
    return tuple(overrides)


def _config_text(text: str, cache_dir: Path) -> str:
    lines: list[str] = []
    top_level = True
    for line in text.splitlines(keepends=True):
        if line.lstrip().startswith("["):
            top_level = False
        if line.strip().startswith("# Managed by onex lab disk growth (OMN-17427):"):
            continue
        if top_level and re.match(r"\s*(cache-dir|link-mode)\s*=", line):
            continue
        lines.append(line)
    return (
        f"{MANAGED_COMMENT}\ncache-dir = {json.dumps(str(cache_dir))}\n"
        f"link-mode = {json.dumps('hardlink')}\n" + "".join(lines)
    )


def _write_atomic(path: Path, text: str) -> None:
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            os.fchmod(stream.fileno(), mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class HandlerLabUvCacheColocate:
    """Refuse cross-filesystem caches and config edits that alter unrelated TOML."""

    def __init__(
        self,
        login_env: Callable[[], Mapping[str, str]] | None = None,
        device_of: Callable[[Path], int] | None = None,
    ) -> None:
        self.login_env = login_env or _login_env
        self.device_of = device_of or (lambda path: os.stat(path).st_dev)

    def handle(
        self, request: ModelLabUvCacheColocateRequest
    ) -> ModelLabUvCacheColocateResult:
        if not request.worktrees_root.exists():
            return ModelLabUvCacheColocateResult(
                status="refused", reason="worktrees_root is missing"
            )
        ancestor = request.cache_dir
        while not ancestor.exists():
            ancestor = ancestor.parent
        same_device = self.device_of(request.worktrees_root) == self.device_of(ancestor)
        overrides = _overrides(self.login_env(), request.cache_dir)

        def result(status: str, reason: str = "") -> ModelLabUvCacheColocateResult:
            return ModelLabUvCacheColocateResult(
                status=status,
                reason=reason,
                cache_dir=str(request.cache_dir),
                env_overrides=overrides,
            )

        if not same_device:
            return result(
                "refused",
                "cache is on another filesystem; hardlinks cannot cross filesystems",
            )
        try:
            text = (
                request.uv_config.read_text(encoding="utf-8")
                if request.uv_config.exists()
                else ""
            )
            original = tomllib.loads(text)
        except (OSError, ValueError) as exc:
            return result("refused", f"cannot parse uv config: {exc}"[:300])
        wanted = {
            **original,
            "cache-dir": str(request.cache_dir),
            "link-mode": "hardlink",
        }
        if request.dry_run:
            reason = (
                "already configured"
                if original == wanted
                else f"would set cache-dir to {request.cache_dir} and link-mode to hardlink"
            )
            return result("dry-run", reason)
        if original == wanted:
            return result("unchanged")
        edited = _config_text(text, request.cache_dir)
        try:
            if tomllib.loads(edited) != wanted:
                return result("refused", "config edit would change unrelated settings")
        except ValueError as exc:
            return result("refused", f"config edit is not valid TOML: {exc}"[:300])
        try:
            request.cache_dir.mkdir(parents=True, exist_ok=True)
            _write_atomic(request.uv_config, edited)
        except OSError as exc:
            return result("refused", f"cannot configure uv cache: {exc}"[:300])
        return result("configured")
