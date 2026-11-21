# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Installed hook → installed CLI → existing local model, with a failure control.

Failure modes: a fixture substituted for inference, an ambient paid route,
missing run/model provenance, a different source revision, or a missing CLI
mistaken for an answer. Normal CI skips this opt-in probe; its contract command
loads tests.refuse_skips explicitly, so an unexecuted proof fails closed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import socket
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict
from urllib.parse import urlsplit

import pytest

from omnimarket.nodes.node_hook_delegate_effect.adapters.adapter_delegate_cli import (
    HandlerDelegateCli,
    installed_onex,
)
from omnimarket.nodes.node_hook_delegate_effect.container import build_handler
from omnimarket.nodes.node_hook_delegate_effect.models import (
    EnumHookDelegateOutcome,
    ModelHookDelegateRequest,
)

pytestmark = [pytest.mark.integration, pytest.mark.live_model]


class InstalledProvenance(TypedDict):
    launcher: str
    python: str
    package_root: str
    revision: str
    files_checked: int
    source_manifest_sha256: str


def checkout_root() -> Path:
    return Path(
        subprocess.check_output(
            [
                "git",
                "-C",
                str(Path(__file__).resolve().parent),
                "rev-parse",
                "--show-toplevel",
            ],
            text=True,
        ).strip()
    )


def nonce_task(nonce: str) -> str:
    return "Repeat this code exactly and say nothing else: " + nonce


def answer_carries_nonce(answer: str, nonce: str) -> bool:
    return nonce in answer


def installed_provenance(
    launcher: Path,
    revision: str,
    repository: Path,
) -> InstalledProvenance:
    """Bind the launcher's real installed source to committed package bytes.

    Pytest adds checkout src/ to sys.path. A separate isolated interpreter must
    inspect the CLI installation, or a stale installed CLI can pass this proof.
    This verifies tracked package source; the complete wheel journey remains
    the separate packaged-journey check.
    """
    lines = launcher.read_text().splitlines()
    if lines[0].strip() == "#!/bin/sh":
        match = None
        for line in lines:
            match = re.match(r"^'''exec' \"([^\"]+)\" ", line)
            if match:
                break
        assert match is not None, "installed CLI sh launcher names no interpreter"
        python = Path(match.group(1))
    else:
        shebang = lines[0]
        assert shebang.startswith("#!"), "installed CLI needs an interpreter shebang"
        python = Path(shebang[2:].strip())
    assert python.is_absolute(), "installed CLI needs an absolute interpreter shebang"
    assert python.is_file(), "installed CLI interpreter does not exist"

    ls_output = subprocess.check_output(
        [
            "git",
            "-C",
            str(repository),
            "ls-tree",
            "-r",
            "-z",
            revision,
            "--",
            "src/omnimarket",
        ],
    )
    entries: list[tuple[str, str]] = []
    for raw in ls_output.split(b"\0"):
        if not raw:
            continue
        meta, separator, path_bytes = raw.partition(b"\t")
        if not separator:
            continue
        raw_mode, raw_type, raw_oid = meta.split()
        if raw_type != b"blob" or raw_mode not in {b"100644", b"100755"}:
            continue
        entries.append((raw_oid.decode(), path_bytes.decode()))

    cat_input = "".join(f"{oid}\n" for oid, _ in entries).encode()
    cat_output = subprocess.run(
        ["git", "-C", str(repository), "cat-file", "--batch"],
        input=cat_input,
        stdout=subprocess.PIPE,
        check=True,
    ).stdout
    contents: list[bytes] = []
    offset = 0
    for oid, _ in entries:
        header_end = cat_output.index(b"\n", offset)
        header = cat_output[offset:header_end].decode()
        offset = header_end + 1
        found_oid, obj_type, size_text = header.split()
        assert found_oid == oid
        assert obj_type == "blob"
        size = int(size_text)
        content = cat_output[offset : offset + size]
        offset += size
        assert cat_output[offset : offset + 1] == b"\n"
        offset += 1
        contents.append(content)

    expected = {}
    for (_oid, path), content in zip(entries, contents, strict=True):
        relative = path.removeprefix("src/omnimarket/")
        expected[relative] = hashlib.sha256(content).hexdigest()
    assert expected, "committed package source manifest is empty"

    inspect = """
import hashlib, importlib.util, json, pathlib, sys
from importlib.machinery import all_suffixes
import omnimarket
root = pathlib.Path(omnimarket.__file__).resolve().parent
expected = json.load(sys.stdin)
actual = {
    path.relative_to(root).as_posix()
    for path in root.rglob("*")
    if path.is_file()
    and (
        path.relative_to(root).as_posix() in expected
        or any(path.name.endswith(suffix) for suffix in all_suffixes())
    )
    and not (path.suffix == ".pyc" and path.parent.name == "__pycache__")
}
assert actual == set(expected), (
    "package file set mismatch: extra=" + ", ".join(sorted(actual - set(expected))[:5])
    + "; missing=" + ", ".join(sorted(set(expected) - actual)[:5])
)
mismatches = [name for name, digest in expected.items()
              if not (root / name).is_file()
              or hashlib.sha256((root / name).read_bytes()).hexdigest() != digest]
assert not mismatches, "package byte mismatch: " + ", ".join(mismatches[:5])
stale = []
for path in root.rglob("*.pyc"):
    if path.parent.name != "__pycache__":
        continue
    data = path.read_bytes()
    flags = int.from_bytes(data[4:8], "little")
    if flags & 1 and not flags & 2:
        source = path.parent.parent / (path.name.split(".")[0] + ".py")
        if not source.is_file() or data[8:16] != importlib.util.source_hash(
            source.read_bytes()
        ):
            stale.append(path.relative_to(root).as_posix())
assert not stale, "stale unchecked bytecode: " + ", ".join(stale[:5])
print(json.dumps({"python": sys.executable, "package_root": str(root)}))
"""
    manifest = json.dumps(expected, sort_keys=True)
    observed = subprocess.run(
        [str(python), "-I", "-c", inspect],
        input=manifest,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert observed.returncode == 0, (
        "installed package differs from revision: " + observed.stderr
    )
    result = json.loads(observed.stdout)
    return {
        "launcher": str(launcher),
        "python": str(result["python"]),
        "package_root": str(result["package_root"]),
        "revision": revision,
        "files_checked": len(expected),
        "source_manifest_sha256": hashlib.sha256(manifest.encode()).hexdigest(),
    }


def test_installed_hook_answers_with_a_local_model_and_missing_cli_passes_through(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if os.environ.get("OMN_ALLOW_LIVE_LADDER") != "1":
        pytest.skip("requires the explicitly authorized local-model lab run")
    url = os.environ.get("ONEX_HOOK_PROOF_LOCAL_URL", "")
    model = os.environ.get("ONEX_HOOK_PROOF_LOCAL_MODEL", "")
    expected_sha = os.environ.get("ONEX_HOOK_PROOF_SHA", "")
    parsed = urlsplit(url)
    assert parsed.scheme == "http"
    assert parsed.hostname in {"127.0.0.1", "::1"}
    assert parsed.username is None
    assert parsed.password is None
    assert not parsed.query
    assert not parsed.fragment
    assert parsed.path in {"", "/"}
    assert model, "name the existing local model"
    assert expected_sha, "name the pushed SHA"
    revision = subprocess.check_output(
        ["git", "-C", str(checkout_root()), "rev-parse", "HEAD"],
        text=True,
    ).strip()
    assert revision == expected_sha, "proof must run the exact pushed revision"
    onex = installed_onex()
    assert onex == Path(sys.executable).parent / "onex"
    assert onex.is_file(), "the branch installation must include its own CLI"
    installed = installed_provenance(onex, expected_sha, checkout_root())

    # No workspace overlay, provider key, user config or cached answer can leak
    # into this run. Local-only is set BEFORE onboarding's own pinned probe.
    keep = {"PATH", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR"}
    for key in tuple(os.environ):
        if key not in keep:
            monkeypatch.delenv(key)
    for key, directory in {
        "HOME": tmp_path / "home",
        "XDG_CONFIG_HOME": tmp_path / "config",
        "XDG_CACHE_HOME": tmp_path / "cache",
        "XDG_STATE_HOME": tmp_path / "state",
        "ONEX_STATE_DIR": tmp_path / "onex-state",
    }.items():
        directory.mkdir()
        monkeypatch.setenv(key, str(directory))
    monkeypatch.chdir(tmp_path)
    for args in (
        ["models", "only", "local"],
        ["models", "add", "local", "--url", url, "--model", model, "--json"],
    ):
        completed = subprocess.run(
            [str(onex), *args],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr

    nonce = "omn24723-" + secrets.token_hex(4)
    request = ModelHookDelegateRequest(
        task=nonce_task(nonce),
        timeout_seconds=90,
        system_prompt="Repeat the code you are given exactly and say nothing else.",
        criteria=("response_non_empty", "concise", "final_artifact_only"),
        criteria_mode="replace-task-class",
        max_tokens=128,
    )
    result = build_handler().handle(request)
    assert result.outcome is EnumHookDelegateOutcome.ANSWERED
    assert result.answer.strip()
    assert answer_carries_nonce(result.answer, nonce), (
        "the answer does not carry this run's code, so no model read the request"
    )
    assert result.model == model
    assert result.run_id
    missing = build_handler(HandlerDelegateCli(onex=tmp_path / "absent-onex"))
    fallback = missing.handle(request)
    assert fallback.outcome is EnumHookDelegateOutcome.NO_ONEX
    assert not fallback.answered
    assert not fallback.answer
    assert not fallback.run_id
    proof = {
        "observed_at": datetime.now(UTC).isoformat(),
        "host": socket.gethostname(),
        "revision": revision,
        "installed": installed,
        "model": result.model,
        "run_id": result.run_id,
        "outcome": result.outcome.value,
        "error_control": fallback.outcome.value,
        "answer": result.answer,
        "answer_sha256": hashlib.sha256(result.answer.encode()).hexdigest(),
        "nonce": nonce,
    }
    (tmp_path / "hook-integration-proof.json").write_text(json.dumps(proof, indent=2))
    sys.stdout.write(
        "HOOK_INTEGRATION_PROOF " + json.dumps(proof, sort_keys=True) + "\n"
    )
