# SPDX-License-Identifier: MIT
"""Run TLC over the landing_stall model matrix and write formal/landing_stall/results/.

One ``<config>.out`` per config and ``SUMMARY.txt`` with the sha256 of the model, each
config and each config's output. The design configs must exit 0 with "No error has been
found"; each mutant must report a counterexample to the one property its config
checks. ``tests/formal/test_landing_stall_model.py`` reads SUMMARY.txt and fails when the
model or a config changes without a re-run.

Usage: ``python3 tests/formal/landing_stall_tlc.py`` (always the whole matrix).

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

MODEL_DIR = Path(__file__).resolve().parents[2] / "formal" / "landing_stall"
MODEL = "LandingStall.tla"
JAR_SHA = "936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88"
JAR_URL = "https://github.com/tlaplus/tlaplus/releases/download/v1.7.4/tla2tools.jar"
MATRIX = (
    "LandingStall_design",
    "LandingStall_design_live",
    "M1_no_min_prs",
    "M2_strict_share",
    "M3_strict_count",
    "M4_key_per_snapshot",
    "M5_escalation_immediate",
    "M6_escalation_repeats",
    "M7_no_cause_lane",
    "M8_no_block",
    "M9_block_all_contexts",
    "M10_block_at_open",
    "M11_block_outlives_close",
    "M12_question_immediate",
    "M13_question_ignores_repair",
    "M14_question_repeats",
    "M15_close_on_one_read",
    "M16_never_close",
    "M17_never_escalate",
    "M18_clock_resets",
    "W1_stall_reaches_escalation",
    "W2_stall_reaches_question",
    "W3_stall_closes_after_escalation",
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
