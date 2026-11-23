"""OMN-26405: the market runtime image rebuilds the prebuilt node manifest after its own installs.

The infra image stamps the manifest, then this layer uninstalls and reinstalls omnimarket
and strips the entry points, so the stamp's distribution listing is stale on every boot.
These tests read ``Dockerfile.runtime`` and prove four things: a manifest build step
exists, it comes after every instruction that changes an installed distribution or a
contract file, it takes its active package set and workload from the base image's own
stamp (a build argument wins), and it fails closed.

Source of the active set and workload: the stamp of the manifest the base image
carries, read with the infra model; not a literal in this file.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
BUILD_ENTRY = "runtime_prebuilt_manifest build"
STRIP_ENTRY = "-m omnibase_infra.runtime.strip_runtime_entry_points"
RUN_OPEN = "RUN <<'MANIFEST'"
RUN_CLOSE = "MANIFEST"
INSTRUCTION = re.compile(r"^(ARG|COPY|ENV|FROM|LABEL|RUN|USER)\b")
HEREDOC = re.compile(r"<<-?['\"]?([A-Za-z_]+)['\"]?")


@pytest.fixture
def dockerfile() -> str:
    return (ROOT / "Dockerfile.runtime").read_text()


def instructions(dockerfile: str) -> list[str]:
    """Each instruction with its continuation lines and heredoc body attached."""
    out: list[str] = []
    lines = dockerfile.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if not INSTRUCTION.match(line):
            i += 1
            continue
        block = [line]
        tags = HEREDOC.findall(line)
        while block[-1].endswith("\\") and i + 1 < len(lines):
            i += 1
            block.append(lines[i])
        while tags:
            tag = tags.pop(0)
            i += 1
            while i < len(lines):
                block.append(lines[i])
                if lines[i] == tag:
                    break
                i += 1
        out.append("\n".join(block))
        i += 1
    return out


def manifest_index(steps: list[str]) -> int:
    matches = [n for n, step in enumerate(steps) if BUILD_ENTRY in step]
    assert len(matches) == 1, f"{len(matches)} manifest build steps"
    assert steps[matches[0]].startswith("RUN ")
    return matches[0]


def changes_stamp_input(step: str) -> bool:
    """An installed distribution, an entry point or a contract/config file changes."""
    if step.startswith("COPY "):
        return True
    if not step.startswith("RUN "):
        return False
    return (
        "uv pip" in step
        or "pip install" in step
        or "pip uninstall" in step
        or STRIP_ENTRY in step
    )


def manifest_script(dockerfile: str) -> str:
    lines = dockerfile.splitlines()
    assert lines.count(RUN_OPEN) == 1
    start = lines.index(RUN_OPEN) + 1
    end = lines.index(RUN_CLOSE, start)
    return "\n".join(lines[start:end]) + "\n"


@pytest.mark.unit
def test_omnimarket_manifest_after_install_step_exists(dockerfile: str) -> None:
    steps = instructions(dockerfile)
    index = manifest_index(steps)
    assert "omnibase_infra.nodes.node_boot_catalog_effect" in steps[index]
    assert '--workload "${workload}"' in steps[index]


@pytest.mark.unit
def test_omnimarket_manifest_after_install_comes_after_every_stamp_input(
    dockerfile: str,
) -> None:
    steps = instructions(dockerfile)
    index = manifest_index(steps)
    strip = [n for n, step in enumerate(steps) if STRIP_ENTRY in step]
    assert len(strip) == 1
    assert index > strip[0]
    mutating = [n for n, step in enumerate(steps) if changes_stamp_input(step)]
    assert mutating, "no install or copy step found: the parser read nothing"
    assert index > max(mutating), steps[max(mutating)]
    # Nothing after the manifest step changes an installed distribution or a file.
    assert not [s for s in steps[index + 1 :] if changes_stamp_input(s)]
    assert not [s for s in steps[index + 1 :] if s.startswith("RUN ")]


@pytest.mark.unit
def test_omnimarket_manifest_after_install_takes_set_and_workload_from_the_stamp(
    dockerfile: str,
) -> None:
    script = manifest_script(dockerfile)
    assert "ModelPrebuiltNodeManifest.model_validate_json" in script
    assert "prebuilt_manifest_path()" in script
    assert "stamp.active_packages" in script
    assert "stamp.workload" in script
    assert 'ONEX_ACTIVE_RUNTIME_PACKAGES="${active}"' in script
    # No literal package set is decided here.
    assert not re.search(r"omnibase_infra,\w+", script)
    assert "ARG PREBUILT_ACTIVE_RUNTIME_PACKAGES" in dockerfile
    assert "ARG PREBUILT_WORKLOAD" in dockerfile


FAKE_PYTHON = """#!/bin/sh
# Stand-in for the image interpreter: "-" reads the stamp, "-m" is the build.
if [ "$1" = "-" ]; then
  cat >/dev/null
  [ "${FAKE_READER_FAILS:-}" = "1" ] && { echo "no manifest" >&2; exit 1; }
  printf '%s\\n%s\\n' "${FAKE_STAMP_ACTIVE-omnibase_infra,omnimarket}" "${FAKE_STAMP_WORKLOAD-main}"
  exit 0
fi
printf 'ACTIVE=%s ARGS=%s\\n' "${ONEX_ACTIVE_RUNTIME_PACKAGES-unset}" "$*" >>"${FAKE_BUILD_LOG}"
"""


def run_script(
    dockerfile: str, tmp_path: Path, **env: str
) -> tuple[subprocess.CompletedProcess[str], str]:
    fake = tmp_path / "python"
    fake.write_text(FAKE_PYTHON)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "build.log"
    script = manifest_script(dockerfile).replace("/app/.venv/bin/python", str(fake))
    result = subprocess.run(
        ["sh", "-c", script],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
        env={"PATH": os.environ["PATH"], "FAKE_BUILD_LOG": str(log), **env},
    )
    return result, log.read_text() if log.exists() else ""


@pytest.mark.unit
def test_omnimarket_manifest_after_install_builds_with_the_stamped_set(
    dockerfile: str, tmp_path: Path
) -> None:
    result, log = run_script(
        dockerfile,
        tmp_path,
        FAKE_STAMP_ACTIVE="omnibase_infra,omnimarket",
        FAKE_STAMP_WORKLOAD="main",
    )
    assert result.returncode == 0, result.stderr
    assert log == (
        "ACTIVE=omnibase_infra,omnimarket ARGS=-m "
        "omnibase_infra.nodes.node_boot_catalog_effect.runtime_prebuilt_manifest "
        "build --workload main\n"
    )


@pytest.mark.unit
def test_omnimarket_manifest_after_install_build_argument_wins(
    dockerfile: str, tmp_path: Path
) -> None:
    result, log = run_script(
        dockerfile,
        tmp_path,
        PREBUILT_ACTIVE_RUNTIME_PACKAGES="omnibase_infra,omnimarket,omniclaude",
        PREBUILT_WORKLOAD="side",
    )
    assert result.returncode == 0, result.stderr
    assert "ACTIVE=omnibase_infra,omnimarket,omniclaude " in log
    assert log.endswith("build --workload side\n")


@pytest.mark.unit
@pytest.mark.parametrize(
    "env",
    [
        {"FAKE_STAMP_ACTIVE": ""},
        {"FAKE_STAMP_ACTIVE": "omnibase_infra, omnimarket"},
        {"FAKE_STAMP_WORKLOAD": ""},
        {"FAKE_READER_FAILS": "1"},
    ],
    ids=["empty-set", "spaced-set", "empty-workload", "no-stamp"],
)
def test_omnimarket_manifest_after_install_fails_closed(
    dockerfile: str, tmp_path: Path, env: dict[str, str]
) -> None:
    result, log = run_script(dockerfile, tmp_path, **env)
    assert result.returncode != 0
    assert log == "", "the build ran with a missing set or workload"
    assert "OMN-26405" in result.stderr or "no manifest" in result.stderr
