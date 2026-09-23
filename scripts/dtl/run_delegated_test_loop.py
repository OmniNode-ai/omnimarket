#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run one delegated test loop in-process (OMN-19362, first slice).

Binds the orchestrator's ports to the real children and prints ONE compact
result as JSON on stdout. Nothing else goes to stdout.

* prompt   -> node_delegated_test_prompt_compute (pure, in-process)
* delegate -> the sanctioned ``onex`` wrapper, ``onex delegate ... --bus
              inmemory --locus in-process``: the delegate orchestrator runs
              here and only the model call leaves the machine. Each call writes
              its own receipt under ``<state root>/runs/<run id>/``.
* run      -> node_push_validation_effect ``run_focused_test_run``: the test
              executes in a throwaway single-mount container on the lab host
              named by ``ONEX_DTL_HOST`` (the effect's own required variables).
* digest   -> node_pytest_failure_digest_compute (pure, in-process)
* grade    -> node_delegated_test_control_compute (pure, in-process)

The loop receipt is ``<state root>/runs/<correlation id>/loop_receipt.json``,
with each focused-run receipt beside it under ``focused/``.

Usage:
    uv run python scripts/dtl/run_delegated_test_loop.py --request <request.json> \
        --state-root <state root> --source-clone <local clone of the repository>

``OMNI_HOME`` must be set: the ``onex`` wrapper is resolved from it.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Literal

from omnimarket.nodes.node_delegated_test_control_compute import (
    ModelControlGradeRequest,
    grade_control,
)
from omnimarket.nodes.node_delegated_test_prompt_compute import (
    ModelDelegatedTestPromptRequest,
    ModelFailureContext,
    build_prompt_bundle,
)
from omnimarket.nodes.node_push_validation_effect.models.model_focused_test_run_request import (
    ModelFocusedTestRunRequest,
    ModelSourceMutation,
)
from omnimarket.nodes.node_pytest_failure_digest_compute import (
    ModelPytestRunReport,
    digest_pytest_run,
)

from omnimarket.nodes.node_delegated_test_loop_orchestrator import (
    HandlerDelegatedTestLoopOrchestrator,
    LoopReceiptExistsError,
    ModelControlVerdict,
    ModelDelegatedTestLoopRequest,
    ModelDelegateReply,
    ModelPrompt,
    ModelRunDigest,
    ModelRunReceipt,
    parse_test_reply,
)
from omnimarket.nodes.node_push_validation_effect import HandlerFocusedTestRunEffect

_DELEGATE_TIMEOUT_SECONDS = 900
_HOST_BUSY_BACKOFF_SECONDS = 30


class InProcessLoopPorts:
    def __init__(
        self, omni_home: Path, state_root: Path, source_clone: Path, test_path: str
    ) -> None:
        self._test_path = test_path
        self._onex = omni_home / "omnibase_infra" / "scripts" / "onex"
        self._state_root = state_root
        self._source_clone = source_clone
        self._focused = HandlerFocusedTestRunEffect()
        self._loop_dir: Path | None = None

    def read_target(self, repo: str, ref: str, path: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self._source_clone), "show", f"{ref}:{path}"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout

    def build_prompt(
        self,
        request: ModelDelegatedTestLoopRequest,
        target_excerpt: str,
        previous_test: str,
        last: ModelRunDigest | None,
    ) -> ModelPrompt:
        failure = (
            None
            if last is None
            else ModelFailureContext(
                outcome=last.outcome,
                exception_type=last.exception_type[:200],
                message=last.message,
                frames=last.frames,
                failing_node_id=last.failing_node_id[:500],
            )
        )
        bundle = build_prompt_bundle(
            ModelDelegatedTestPromptRequest(
                mode="write" if last is None else "repair",
                criterion=request.criterion,
                target_path=request.target_path,
                target_excerpt=target_excerpt,
                test_path=request.test_path,
                previous_test=previous_test,
                failure=failure,
                forbidden_fragments=request.forbidden_fragments,
            )
        )
        return ModelPrompt(
            prompt=bundle.prompt, response_contract=bundle.response_contract
        )

    def delegate(self, prompt: ModelPrompt, attempt: int) -> ModelDelegateReply:
        argv = [
            str(self._onex),
            "delegate",
            prompt.prompt,
            "--task-type",
            "test",
            "--response-contract",
            json.dumps(prompt.response_contract),
            "--bus",
            "inmemory",
            "--locus",
            "in-process",
            "--state-root",
            str(self._state_root),
        ]
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=_DELEGATE_TIMEOUT_SECONDS,
            check=False,
        )
        run_id = ""
        for line in reversed(result.stdout.splitlines()):
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict) and payload.get("run_id"):
                run_id = str(payload["run_id"])
                break
        if not run_id:
            match = re.search(r"runs/([0-9a-f-]{36})/receipt\.json", result.stderr)
            run_id = match.group(1) if match else ""
        text, tokens_in, tokens_out, model = "", 0, 0, ""
        run_dir = self._state_root / "runs" / run_id
        if run_id and (run_dir / "result.txt").is_file():
            text = (run_dir / "result.txt").read_text()
        if run_id and (run_dir / "receipt.json").is_file():
            receipt = json.loads((run_dir / "receipt.json").read_text())
            model = str(receipt.get("model", ""))
            # A deployed-lane receipt nests the terminal under terminal_payload;
            # an in-process (--bus inmemory) receipt carries it as result.
            result_block = receipt.get("receipt", {}).get("result", {})
            terminal = result_block.get("terminal_payload", {}).get(
                "payload", result_block
            )
            metrics = terminal.get("metrics", {}) if isinstance(terminal, dict) else {}
            tokens_in = int(metrics.get("input_tokens") or 0)
            tokens_out = int(metrics.get("output_tokens") or 0)
        if result.returncode != 0 and not text:
            return ModelDelegateReply(
                run_id=run_id,
                ok=False,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                model=model,
                invalid_reason=f"onex delegate exited {result.returncode}: {result.stderr[-300:]}",
            )
        source, reason = parse_test_reply(text, self._test_path)
        return ModelDelegateReply(
            run_id=run_id,
            ok=not reason,
            test_source=source,
            invalid_reason=reason,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            model=model,
        )

    def run(
        self,
        request: ModelDelegatedTestLoopRequest,
        ref: str,
        ref_role: Literal["fixed", "prefix", "mutation"],
        attempt: int,
        test_source: str,
    ) -> ModelRunReceipt:
        receipt = self._focused.run_sync(
            ModelFocusedTestRunRequest(
                repo=request.repo,
                commit_sha=ref,
                overlay_files={request.test_path: test_source},
                hide_paths=request.hide_paths,
                mutations=tuple(
                    ModelSourceMutation(path=m.path, find=m.find, replace=m.replace)
                    for m in request.mutations
                )
                if ref_role == "mutation"
                else (),
                test_node_id=request.test_path,
                timeout_seconds=request.timeout_seconds,
                correlation_id=request.correlation_id,
                ref_role=ref_role,
                attempt=attempt,
            )
        )
        receipt_id = f"{request.correlation_id[:8]}-{ref_role}-a{attempt}"
        if receipt.status.value == "host_busy":
            receipt_id += f"-busy{int(time.time())}"
        if self._loop_dir is not None:
            focused = self._loop_dir / "focused"
            focused.mkdir(exist_ok=True)
            (focused / f"{receipt_id}.json").write_text(
                json.dumps(receipt.model_dump(mode="json"), indent=1)
            )
        return ModelRunReceipt(
            receipt_id=receipt_id,
            status=receipt.status.value,  # type: ignore[arg-type]
            exit_code=receipt.exit_code,
            junit_xml=receipt.junit_xml,
            detail=receipt.detail,
        )

    def digest(self, receipt: ModelRunReceipt) -> ModelRunDigest:
        digest = digest_pytest_run(
            ModelPytestRunReport(
                junit_xml=receipt.junit_xml, exit_code=receipt.exit_code
            )
        )
        return ModelRunDigest(
            receipt_id=receipt.receipt_id,
            receipt_status=receipt.status,
            outcome=digest.outcome.value,  # type: ignore[arg-type]
            exception_type=digest.exception_type,
            message=digest.message,
            frames=digest.frames,
            top_frame=digest.top_frame,
            failing_node_id=digest.failing_node_id,
            fingerprint=digest.fingerprint,
        )

    def grade(
        self,
        prefix_outcome: str,
        mutation_outcome: str | None,
        mutation_requested: bool,
        prefix_ref_equals_fixed_ref: bool,
    ) -> ModelControlVerdict:
        grade = grade_control(
            ModelControlGradeRequest(
                fixed_outcome="passed",
                prefix_outcome=prefix_outcome,  # type: ignore[arg-type]
                mutation_outcome=mutation_outcome,  # type: ignore[arg-type]
                mutation_requested=mutation_requested,
                prefix_ref_equals_fixed_ref=prefix_ref_equals_fixed_ref,
            )
        )
        return ModelControlVerdict(
            status=grade.status.value,
            headline=grade.headline,
            control_ref_role=grade.control_ref_role,
            control_outcome=grade.control_outcome,
        )

    def wait_for_host(self, tries: int) -> None:
        time.sleep(_HOST_BUSY_BACKOFF_SECONDS * tries)

    def claim_loop_receipt(self, loop_run_id: str) -> None:
        loop_dir = self._state_root / "runs" / loop_run_id
        if (loop_dir / "loop_receipt.json").exists():
            raise LoopReceiptExistsError(f"a loop receipt exists for {loop_run_id}")
        loop_dir.mkdir(parents=True, exist_ok=True)
        self._loop_dir = loop_dir

    def write_loop_receipt(self, loop_run_id: str, payload: dict[str, object]) -> None:
        path = self._state_root / "runs" / loop_run_id / "loop_receipt.json"
        path.write_text(json.dumps(payload, indent=1))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--state-root", required=True, type=Path)
    parser.add_argument("--source-clone", required=True, type=Path)
    args = parser.parse_args()

    request = ModelDelegatedTestLoopRequest.model_validate_json(
        args.request.read_text()
    )
    ports = InProcessLoopPorts(
        Path(os.environ["OMNI_HOME"]),
        args.state_root.resolve(),
        args.source_clone.resolve(),
        request.test_path,
    )
    result = HandlerDelegatedTestLoopOrchestrator(ports).run(request)
    sys.stdout.write(
        json.dumps(result.model_dump(mode="json"), separators=(",", ":")) + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
