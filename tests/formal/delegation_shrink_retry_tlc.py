# SPDX-License-Identifier: MIT
"""Run TLC over the delegation_shrink_retry model matrix and write formal/delegation_shrink_retry/results/.

One ``<config>.out`` per config and ``SUMMARY.txt`` with the sha256 of the model, each
config and each config's output. The design configs must exit 0 with "No error has been
found"; each mutant must report a counterexample to the one property its config
checks. ``tests/formal/test_delegation_shrink_retry_model.py`` reads SUMMARY.txt and fails when the
model or a config changes without a re-run.

Usage: ``python3 tests/formal/delegation_shrink_retry_tlc.py`` (always the whole matrix).

tla2tools.jar: ``TLA2TOOLS_JAR`` when set, else the pinned release download (TLC 2.19,
v1.7.4); the sha256 is checked either way. Java: ``JAVA``, then ``java`` on PATH, then
the eclipse-temurin:17-jre-alpine image through the container runtime.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

MODEL_DIR = Path(__file__).resolve().parents[2] / "formal" / "delegation_shrink_retry"
MODEL = "DelegationShrinkRetry.tla"
JAR_SHA = "936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88"
JAR_URL = "https://github.com/tlaplus/tlaplus/releases/download/v1.7.4/tla2tools.jar"
MATRIX = (
    "design",
    "design_no_second_met",
    "design_not_decomposable",
    "design_neither_rung",
    "M1_unbounded_split",
    "M2_accept_without_whole_check",
    "M3_second_terminal_on_redelivery",
    "M4_uncapped_swap",
    "M5_shrink_before_swap",
    "M6_uncapped_unit_attempts",
    "M7_escalate_without_trying_rungs",
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _jar() -> Path:
    jar = MODEL_DIR / "tla2tools.jar"
    if not jar.exists():
        source = os.environ.get("TLA2TOOLS_JAR")
        if source:
            shutil.copy(source, jar)
        else:
            urllib.request.urlretrieve(JAR_URL, jar)
    if _sha(jar) != JAR_SHA:
        raise SystemExit("tla2tools.jar does not match the pinned sha256")
    return jar


def _java() -> list[str]:
    if os.environ.get("JAVA"):
        return [os.environ["JAVA"]]
    if shutil.which("java"):
        return ["java"]
    if shutil.which("docker"):
        user = f"{os.getuid()}:{os.getgid()}"
        mount = f"{MODEL_DIR}:/work"
        return [
            "docker", "run", "--rm", "--user", user, "-v", mount, "-w", "/work",
            "eclipse-temurin:17-jre-alpine", "java",
        ]  # fmt: skip
    raise SystemExit("no working java")


def _tlc(java: list[str], name: str) -> None:
    heap = os.environ.get("HEAP", "4g")
    workers = os.environ.get("WORKERS", "2")
    proc = subprocess.run(
        [
            *java, "-XX:+UseParallelGC", f"-Xmx{heap}", "-cp", "tla2tools.jar",
            "tlc2.TLC", "-config", f"{name}.cfg", "-workers", workers,
            "-metadir", f"states/{name}", "-cleanup", MODEL,
        ],
        cwd=MODEL_DIR, capture_output=True, text=True, check=False,
    )  # fmt: skip
    text = (
        (proc.stdout + proc.stderr).replace(f"{MODEL_DIR}/", "").replace("/work/", "")
    )
    (MODEL_DIR / "results" / f"{name}.out").write_text(
        f"{text}exit={proc.returncode}\n", encoding="utf-8"
    )
    shutil.rmtree(MODEL_DIR / "states" / name, ignore_errors=True)


def _verdict(name: str) -> str:
    out = (MODEL_DIR / "results" / f"{name}.out").read_text(encoding="utf-8")
    if re.search(r"^exit=0$", out, re.MULTILINE) and "No error has been found" in out:
        return "PASS"
    invariant = re.search(r"Invariant ([A-Za-z0-9_]+) is violated", out)
    if invariant:
        return f"VIOLATED {invariant.group(1)}"
    if "Temporal properties were violated" in out:
        cfg = (MODEL_DIR / f"{name}.cfg").read_text(encoding="utf-8")
        prop = re.search(r"^PROPERTY (\w+)", cfg, re.MULTILINE)
        return f"VIOLATED {prop.group(1) if prop else '?'}"
    return "ERROR"


def main() -> int:
    _jar()
    java = _java()
    (MODEL_DIR / "results").mkdir(exist_ok=True)
    lines = [f"model: {MODEL} sha256={_sha(MODEL_DIR / MODEL)}"]
    for name in MATRIX:
        _tlc(java, name)
        out = MODEL_DIR / "results" / f"{name}.out"
        lines.append(
            f"{name} cfg_sha256={_sha(MODEL_DIR / f'{name}.cfg')} "
            f"out_sha256={_sha(out)} result={_verdict(name)}"
        )
    summary = MODEL_DIR / "results" / "SUMMARY.txt"
    summary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    sys.stdout.write(summary.read_text(encoding="utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
