# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Customer clean-install delegation probe (OMN-16200).

A customer installs the published packages on their own machine and runs
``onex delegate`` against their own local model. Until OMN-16200 that first
command refused on ``DELEGATION_ROUTING_TIERS_PATH`` and then on the bifrost
contract/overlay pair: two env vars no public document names, pointing at files
that ship inside the wheel.

This probe is the executable form of that customer. It is given an ``onex``
from a venv the caller built from the wheel under test plus the package index
(never an editable install, never this checkout on ``sys.path``), and it runs
every command under an environment built from nothing -- ``HOME``, ``PATH``,
``LANG`` -- with a fresh ``HOME`` and a working directory outside any checkout.
The local model is an OpenAI-compatible stub on loopback that records every
request, so "the delegation reached the customer's model" is a measurement.

It asserts, in order:

1. ``onex local init`` mints the install's identity.
2. With no model declared anywhere, ``onex delegate`` refuses, reaches no model,
   and the refusal names the overlay file the customer has to write.
3. With the customer's one file written -- their model, in
   ``~/.omninode/delegation/bifrost_overrides.yaml`` -- ``onex delegate``
   completes on that model, with no env var bound.
4. With that model declared on ONE local rung, the way the no-model refusal's
   own example writes it, and no provider key, ``onex delegate "say ok"``
   completes on that model (OMN-19442). The prompt is auto-classed
   ``document``, whose only local rung is a different one; on 2026-09-24 this
   exact install refused it for a missing provider key.

Exit 0 == a clean install delegates out of the box; 1 == it does not; 2 == the
probe could not run.

    python scripts/ci/customer_clean_install_delegate_probe.py --onex <venv>/bin/onex
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import threading
from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Final

#: The model id the stub serves and the customer declares.
SERVED_MODEL: Final[str] = "customer-local-model"

#: The answer the stub gives a delegation. Shaped to pass the shipped
#: ``document`` class's deterministic floors: one plain sentence, no preamble.
STUB_ANSWER: Final[str] = (
    "The sky is blue because Rayleigh scattering by air molecules scatters "
    "short blue wavelengths of sunlight far more strongly than red ones."
)

#: A model following the prompt's instruction opens its answer with a declared
#: answer marker (``task_class_contracts.v1.yaml``); the product strips it.
STUB_REPLY: Final[str] = f"### ANSWER\n{STUB_ANSWER}"

#: The judge's reply, when the reviewer leg reaches the local model.
STUB_JUDGE_ANSWER: Final[str] = '{"adequacy_score": 0.95, "reasoning": "adequate"}'

PROMPT: Final[str] = (
    "Summarize in one sentence: the sky is blue because of Rayleigh scattering."
)

#: The first thing a new user types (OMN-19442), and the model's real answer to
#: it: measured on 2026-09-24 against a served Qwen3.8-27B, "ok" behind the
#: declared answer marker the product strips.
TWO_WORD_PROMPT: Final[str] = "say ok"
TWO_WORD_ANSWER: Final[str] = "ok"
TWO_WORD_REPLY: Final[str] = f"### ANSWER\n{TWO_WORD_ANSWER}"

#: The one local rung the no-model refusal's example declares.
SINGLE_DECLARED_RUNG: Final[str] = "local-coder"

#: The env keys a clean install must NOT need. Checked against the environment
#: every onex command actually ran under.
FORBIDDEN_ENV_KEYS: Final[frozenset[str]] = frozenset(
    {
        "OMNI_HOME",
        "DELEGATION_ROUTING_TIERS_PATH",
        "BIFROST_CONTRACT_PATH",
        "BIFROST_OVERLAY_PATH",
        "TASK_CLASS_CONTRACT_PATH",
        "PYTHONPATH",
    }
)

UNDECLARED_MODEL_MARKER: Final[str] = "No local model is declared"


class ProbeError(RuntimeError):
    """The probe could not run: exit 2, never a verdict."""


def loopback_reply_for(messages: list[Mapping[str, object]]) -> str:
    """The loopback model's reply to one chat request.

    The judge gets its verdict. The two-word step (OMN-19442) is recognised by
    its prompt standing whole as one paragraph of the user turn -- the product
    composes that turn as marker sentence, caller prompt and acceptance rules,
    separated by blank lines -- never by a substring, so a prompt that merely
    mentions the two words gets the primary reply.
    """
    system = " ".join(
        str(m.get("content", "")) for m in messages if m.get("role") == "system"
    )
    if "adequacy judge" in system:
        return STUB_JUDGE_ANSWER
    paragraphs = {
        paragraph.strip()
        for m in messages
        if m.get("role") == "user"
        for paragraph in str(m.get("content", "")).split("\n\n")
    }
    if TWO_WORD_PROMPT in paragraphs:
        return TWO_WORD_REPLY
    return STUB_REPLY


class _StubModel:
    """An OpenAI-compatible model server on loopback that records requests."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        stub = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: object) -> None:
                return

            def _reply(self, payload: Mapping[str, object]) -> None:
                body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                stub.requests.append({"method": "GET", "path": self.path})
                if self.path.startswith("/v1/models"):
                    self._reply({"object": "list", "data": [{"id": SERVED_MODEL}]})
                else:
                    self._reply({"status": "ok"})

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length) or b"{}")
                messages = body.get("messages") or []
                system = " ".join(
                    str(m.get("content", ""))
                    for m in messages
                    if m.get("role") == "system"
                )
                is_judge = "adequacy judge" in system
                stub.requests.append(
                    {
                        "method": "POST",
                        "path": self.path,
                        "model": body.get("model"),
                        "judge": is_judge,
                        "authorization": self.headers.get("Authorization") is not None,
                    }
                )
                content = loopback_reply_for(messages)
                self._reply(
                    {
                        "id": "chatcmpl-probe",
                        "object": "chat.completion",
                        "model": body.get("model"),
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": content},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {
                            "prompt_tokens": 40,
                            "completion_tokens": 25,
                            "total_tokens": 65,
                        },
                    }
                )

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> _StubModel:
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()

    def chat_posts(self) -> list[dict[str, Any]]:
        return [r for r in self.requests if r["method"] == "POST"]


def customer_overlay_yaml(
    port: int,
    backend_ids: tuple[str, ...] = ("local-coder", "local-heavy-reasoning"),
) -> str:
    """The customer's one file: their model, on the named shipped local rungs."""
    endpoint = f"http://127.0.0.1:{port}/v1/chat/completions"  # url-authority-ok: the probe's own loopback stub model
    lines = ["backends:"]
    for backend_id in backend_ids:
        lines += [
            f"  - backend_id: {backend_id}",
            f'    endpoint_url: "{endpoint}"',
            f'    model_name: "{SERVED_MODEL}"',
        ]
    return "\n".join(lines) + "\n"


def _run(
    argv: list[str], *, env: Mapping[str, str], cwd: Path, timeout: int
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        env=dict(env),
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _terminal(stdout: str) -> dict[str, Any]:
    try:
        doc = json.loads(stdout)
    except json.JSONDecodeError:
        return {}
    result = doc.get("result") if isinstance(doc, dict) else None
    if not isinstance(result, dict):
        return {}
    terminal = result.get("terminal_payload")
    if isinstance(terminal, dict):
        return terminal
    return result


def probe(onex: Path, *, timeout: int) -> list[str]:
    """Run the three steps; return the failures (empty == PASS)."""
    if not onex.is_file():
        raise ProbeError(f"no onex at {onex}; build the customer venv first")
    failures: list[str] = []
    with tempfile.TemporaryDirectory(prefix="omn16200-") as scratch:
        root = Path(scratch)
        home = root / "home"
        work = root / "work"
        home.mkdir()
        work.mkdir()
        env = {
            "HOME": str(home),
            "PATH": f"{onex.parent}:/usr/bin:/bin",
            "LANG": "C.UTF-8",
        }
        leaked = sorted(FORBIDDEN_ENV_KEYS & set(env))
        if leaked:
            raise ProbeError(f"probe env carries {leaked}; it must not")

        with _StubModel() as model:
            init = _run(
                [str(onex), "local", "init", "--json"],
                env=env,
                cwd=work,
                timeout=timeout,
            )
            if init.returncode != 0:
                failures.append(
                    f"onex local init exited {init.returncode}: {init.stderr[-800:]}"
                )
                return failures

            refused = _run(
                [str(onex), "delegate", PROMPT], env=env, cwd=work, timeout=timeout
            )
            overlay = home / ".omninode" / "delegation" / "bifrost_overrides.yaml"
            refusal_text = refused.stdout + refused.stderr
            if refused.returncode == 0:
                failures.append(
                    "with no model declared, onex delegate exited 0; it must refuse"
                )
            if UNDECLARED_MODEL_MARKER not in refusal_text:
                failures.append(
                    "the no-model refusal does not say no local model is declared: "
                    + (_terminal(refused.stdout).get("error_message") or "")[:600]
                )
            if str(overlay) not in refusal_text:
                failures.append(f"the no-model refusal does not name {overlay}")
            if model.chat_posts():
                failures.append(
                    f"the refused run reached the model: {model.chat_posts()}"
                )

            overlay.parent.mkdir(parents=True, exist_ok=True)
            overlay.write_text(customer_overlay_yaml(model.port), encoding="utf-8")
            done = _run(
                [str(onex), "delegate", PROMPT], env=env, cwd=work, timeout=timeout
            )
            terminal = _terminal(done.stdout)
            if done.returncode != 0:
                failures.append(
                    f"with the customer's model declared, onex delegate exited "
                    f"{done.returncode}: "
                    + str(terminal.get("error_message") or done.stderr[-800:])[:800]
                )
            if terminal.get("response", "").strip() != STUB_ANSWER:
                failures.append(
                    f"the delegation answer is not the local model's: "
                    f"{str(terminal.get('response'))[:300]!r}"
                )
            delegation_posts = [p for p in model.chat_posts() if not p["judge"]]
            if not delegation_posts:
                failures.append("the delegation never reached the local model")
            if any(p["model"] != SERVED_MODEL for p in delegation_posts):
                failures.append(
                    f"the delegation named a model the customer did not declare: "
                    f"{delegation_posts}"
                )

            # OMN-19442: one rung declared, no key, the two-word prompt.
            overlay.write_text(
                customer_overlay_yaml(model.port, (SINGLE_DECLARED_RUNG,)),
                encoding="utf-8",
            )
            posts_before = len(model.chat_posts())
            short = _run(
                [str(onex), "delegate", TWO_WORD_PROMPT],
                env=env,
                cwd=work,
                timeout=timeout,
            )
            short_terminal = _terminal(short.stdout)
            if short.returncode != 0:
                failures.append(
                    f"with one local model declared, onex delegate "
                    f"{TWO_WORD_PROMPT!r} exited {short.returncode}: "
                    + str(
                        short_terminal.get("error_message")
                        or short_terminal.get("quality_gates_failed")
                        or short.stderr[-800:]
                    )[:800]
                )
            if str(short_terminal.get("response", "")).strip() != TWO_WORD_ANSWER:
                failures.append(
                    f"the {TWO_WORD_PROMPT!r} answer is not the local model's: "
                    f"{str(short_terminal.get('response'))[:300]!r}"
                )
            short_posts = [
                p for p in model.chat_posts()[posts_before:] if not p["judge"]
            ]
            if not short_posts:
                failures.append(
                    f"onex delegate {TWO_WORD_PROMPT!r} never reached the "
                    "declared local model"
                )
            if any(
                p["model"] != SERVED_MODEL or p["authorization"] for p in short_posts
            ):
                failures.append(
                    f"onex delegate {TWO_WORD_PROMPT!r} sent something other "
                    f"than an unauthenticated call to the declared model: "
                    f"{short_posts}"
                )
            print(
                json.dumps(
                    {
                        "model_requests": model.requests,
                        "terminal_status": terminal.get("status"),
                        "quality_gate_passed": terminal.get("quality_gate_passed"),
                        "quality_gates_failed": terminal.get("quality_gates_failed"),
                        "error_message": terminal.get("error_message"),
                        "cost_usd": (terminal.get("metrics") or {}).get("cost_usd"),
                        "two_word": {
                            "exit_code": short.returncode,
                            "task_type": short_terminal.get("task_type"),
                            "terminal_status": short_terminal.get("status"),
                            "response": short_terminal.get("response"),
                            "quality_gates_failed": short_terminal.get(
                                "quality_gates_failed"
                            ),
                            "error_message": short_terminal.get("error_message"),
                        },
                    },
                    indent=2,
                )
            )
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--onex", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args(argv)
    try:
        failures = probe(args.onex, timeout=args.timeout)
    except (ProbeError, subprocess.TimeoutExpired) as exc:
        print(f"::error::customer-clean-install: could not run: {exc}")
        return 2
    for failure in failures:
        print(f"::error::customer-clean-install: {failure}")
    print("FAIL" if failures else "PASS")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
