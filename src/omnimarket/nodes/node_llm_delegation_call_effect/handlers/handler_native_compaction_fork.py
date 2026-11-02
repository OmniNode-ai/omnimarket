# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Native compaction execution inside the sanctioned delegation effect."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from uuid import UUID

import yaml

from omnimarket.models.compaction_replay.model_arm_run import ModelUsage
from omnimarket.models.compaction_replay.model_native_compaction_fork import (
    ModelNativeCompactionBinding,
    ModelNativeCompactionForkRequest,
    ModelNativeCompactionForkResult,
)


def load_native_binding() -> ModelNativeCompactionBinding:
    contract = Path(__file__).resolve().parents[1] / "contract.yaml"
    data = yaml.safe_load(contract.read_text())["native_compaction_fork"]
    state = os.environ.get(data["state_root_env"])
    if not state:
        raise ValueError("native compaction state root is not configured")
    store = os.environ.get(data["session_store_env"])
    if not store:
        config = os.environ.get(data["client_config_env"])
        store = str(
            Path(config) / "projects"
            if config
            else Path.home() / data["session_store_relative"]
        )
    return ModelNativeCompactionBinding(
        workspace_root=Path(state) / data["workspace_subdirectory"],
        session_store=Path(store),
        executable=os.environ.get(data["executable_env"], data["executable"]),
        timeout_s=data["timeout_s"],
    )


class HandlerNativeCompactionFork:
    """Seed only the frozen prefix in a private project, then fork and /compact.

    The configured session store is the client's existing store; no credentials
    are copied or changed. The source prefix and ledger remain untouched. Both
    the fork transcript and the command response are preserved in the workspace.
    """

    def __init__(self, binding: ModelNativeCompactionBinding | None = None) -> None:
        binding = binding if binding is not None else load_native_binding()
        self._executable = binding.executable
        self._store = binding.session_store
        self._timeout = binding.timeout_s
        self._root = binding.workspace_root

    def handle(
        self, request: ModelNativeCompactionForkRequest
    ) -> ModelNativeCompactionForkResult:
        if (
            len(request.transcript_prefix) != request.case.prefix_bytes
            or hashlib.sha256(request.transcript_prefix).hexdigest()
            != request.case.prefix_sha256
        ):
            raise ValueError("native fork prefix does not match the case pin")
        self._root.mkdir(parents=True, exist_ok=True)
        root = Path(tempfile.mkdtemp(prefix="native-fork-", dir=self._root))
        (root / "prefix.jsonl").write_bytes(request.transcript_prefix)
        # UUIDs and a freshly generated cwd prevent source-session overwrite.
        session = str(UUID(request.case.session_id))
        project = re.sub(r"[^a-zA-Z0-9]", "-", str(root.resolve()))
        store = self._store / project
        store.mkdir(parents=True, exist_ok=True)
        with (store / f"{session}.jsonl").open("xb") as seed:
            seed.write(request.transcript_prefix)
        started = time.monotonic()
        completed = subprocess.run(
            [
                self._executable,
                "-p",
                "/compact",
                "--resume",
                session,
                "--fork-session",
                "--output-format",
                "json",
            ],
            cwd=root,
            env={**os.environ, "CLAUDE_CONFIG_DIR": str(self._store.parent)},
            capture_output=True,
            text=True,
            timeout=self._timeout,
            check=False,
        )
        (root / "client-stdout.json").write_text(completed.stdout, encoding="utf-8")
        (root / "client-stderr.txt").write_text(completed.stderr, encoding="utf-8")
        if completed.returncode:
            return ModelNativeCompactionForkResult(
                reply="refused",
                evidence=f"headless /compact exited {completed.returncode}",
                transcript_retained=False,
            )
        response = json.loads(completed.stdout)
        fork_session = str(UUID(response["session_id"]))
        if fork_session == session:
            return ModelNativeCompactionForkResult(
                reply="refused",
                evidence="client did not fork the session",
                transcript_retained=False,
            )
        fork_path = store / f"{fork_session}.jsonl"
        if not fork_path.is_file():
            return ModelNativeCompactionForkResult(
                reply="refused",
                evidence="fork transcript absent",
                transcript_retained=False,
            )
        data = fork_path.read_bytes()
        (root / "fork.jsonl").write_bytes(data)
        records = [json.loads(line) for line in data.splitlines() if line.strip()]
        # Record identity and message content must survive. The client may add
        # fork bookkeeping fields, so byte equality is checked on our frozen
        # input, and semantic equality is checked on the client's copied rows.
        prefix = [
            json.loads(line)
            for line in request.transcript_prefix.splitlines()
            if line.strip()
        ]
        by_id = {r.get("uuid"): r for r in records if r.get("uuid")}
        messages = [r for r in prefix if r.get("type") in {"user", "assistant"}]
        retained = bool(messages) and all(
            r.get("uuid") in by_id
            and by_id[r["uuid"]].get("message") == r.get("message")
            for r in messages
        )
        boundaries = [
            r
            for r in records
            if r.get("subtype") == "compact_boundary"
            and r.get("uuid") not in {p.get("uuid") for p in prefix}
        ]
        if response.get("is_error") or not boundaries or not retained:
            return ModelNativeCompactionForkResult(
                reply="refused",
                evidence="headless fork lacks manual boundary or retained prefix",
                transcript_retained=retained,
            )
        boundary = boundaries[-1]
        metadata = boundary.get("compactMetadata") or {}
        if metadata.get("trigger") != "manual":
            return ModelNativeCompactionForkResult(
                reply="refused", evidence="headless boundary was not manual"
            )
        summaries = [
            r
            for r in records[records.index(boundary) + 1 :]
            if r.get("isCompactSummary")
        ]
        context = None
        if summaries:
            content = summaries[0].get("message", {}).get("content")
            if isinstance(content, str):
                context = content
            elif isinstance(content, list):
                context = "".join(
                    str(b.get("text", "")) for b in content if b.get("type") == "text"
                )
        replacement = json.dumps(records[records.index(boundary) + 1 :])
        custom = tuple(
            marker
            for marker in (
                "activation_id:",
                "installed_messages_hash",
                "custom_boundary",
            )
            if marker in replacement
        )
        if (
            metadata.get("isCustom")
            or metadata.get("compactionSource") == "custom"
            or boundary.get("isCustomSummary")
        ):
            custom = (*custom, "custom boundary metadata")
        # CLI usage is for answering turns and can be zero for a local command;
        # preTokens/postTokens are explicitly estimates of compactor usage.
        return ModelNativeCompactionForkResult(
            reply="native",
            context=context,
            evidence=f"manual boundary in fork {fork_session}",
            custom_boundary_evidence=custom,
            adapter_artifacts=(
                str(root / "fork.jsonl"),
                str(root / "client-stdout.json"),
            ),
            transcript_retained=retained,
            usage=ModelUsage(
                input_tokens=metadata.get("preTokens"),
                post_tokens=metadata.get("postTokens"),
                wall_ms=(time.monotonic() - started) * 1000,
                measurement="estimated",
            ),
        )
