"""Trusted offline acceptance CLI for the existing lab runner, never the compute node.

All tests and rules are frozen by the caller's contract pins. Candidate code is
executed only by the lab runner's isolated worktree process. No provider calls,
network, model judgement, mutable external state, or wall-clock verdicts.
"""

from __future__ import annotations

import argparse
import ast
import importlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


def _code(answer: str) -> str:
    blocks = re.findall(r"```(?:python|py)?\s*\n(.*?)```", answer, re.S)
    return (max(blocks, key=len) if blocks else answer).strip() + "\n"


def _pytest(
    directory: Path, test: Path, env: dict[str, str] | None = None
) -> tuple[int, str]:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "--tb=short",
            "-p",
            "no:cacheprovider",
            str(test),
        ],
        cwd=directory,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return result.returncode, result.stdout + result.stderr


def evaluate(rule: dict[str, Any], answer: str, root: Path) -> bool:
    mode = rule["mode"]
    if not answer.strip():
        return False
    if mode == "phrases":
        content = " ".join(answer.lower().split())
        return all(
            " ".join(phrase.lower().split()) in content for phrase in rule["required"]
        )
    if mode == "summary":
        from omnimarket.delegation.content_grounding import (
            evaluate_numeric_grounding,
            resolve_numeric_grounding_policy,
        )
        from omnimarket.delegation.identifier_grounding import (
            evaluate_identifier_grounding,
            resolve_identifier_grounding_policy,
        )
        from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (
            _check_accurate_summary,
            _check_no_refusal,
            _check_within_sentence_limit,
            _check_within_word_limit,
        )

        prompt = (root / rule["prompt"]).read_text()
        checks = [
            _check_accurate_summary(answer, prompt),
            _check_no_refusal(answer),
            _check_within_sentence_limit(answer, prompt),
            _check_within_word_limit(answer, prompt),
        ]
        nums = evaluate_numeric_grounding(
            content=answer,
            grounding_source=prompt,
            policy=resolve_numeric_grounding_policy(),
        )
        ids = evaluate_identifier_grounding(
            content=answer,
            grounding_source=prompt,
            policy=resolve_identifier_grounding_policy(),
        )
        return (
            all(x is None for x in checks)
            and nums.evaluated
            and not nums.ungrounded
            and ids.evaluated
            and not ids.ungrounded
            and any(cue in answer for cue in rule["cues"])
        )
    if mode in {"solution", "mutation_test"}:
        code = _code(answer)
        try:
            tree = ast.parse(code)
        except SyntaxError:
            return False
        if mode == "mutation_test" and any(
            isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and n.name == rule["symbol"]
            for n in ast.walk(tree)
        ):
            return False
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            test = work / "test_answer.py"
            test.write_text(
                code if mode == "mutation_test" else (root / rule["test"]).read_text()
            )
            (work / "solution.py").write_text(
                code if mode == "solution" else (root / rule["original"]).read_text()
            )
            good, output = _pytest(work, test)
            if good != 0 or not re.search(r"\d+ passed", output):
                return False
            if mode == "solution":
                return True
            (work / "solution.py").write_text((root / rule["mutant"]).read_text())
            for cache in work.glob("__pycache__/*"):
                cache.unlink()
            bad, output = _pytest(work, test)
            return (
                bad == 1
                and bool(re.search(r"\d+ failed", output))
                and "AssertionError" in output
                and "ERROR collecting" not in output
            )
    if mode == "module_tests":
        # Load the candidate under the public module name before frozen pytest collection.
        module_name = rule["module"]
        if module_name.startswith("omnibase_core."):
            # Fresh Python children in the frozen tests must see the candidate too.
            import omnibase_core

            with tempfile.TemporaryDirectory() as temporary:
                work = Path(temporary)
                package = Path(omnibase_core.__file__).parent
                shutil.copytree(
                    package,
                    work / "omnibase_core",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
                )
                source = work / (module_name.replace(".", "/") + ".py")
                source.write_text(answer)
                env = dict(os.environ, PYTHONPATH=str(work))
                test = work / rule["test_layout"]
                test.parent.mkdir(parents=True, exist_ok=True)
                test.write_bytes((root / rule["test"]).read_bytes())
                exit_code, output = _pytest(work, test, env)
                return exit_code == 0 and bool(re.search(r"\d+ passed", output))
        try:
            compile(answer, "<candidate>", "exec")
            parent_name, _, leaf = module_name.rpartition(".")
            parent = importlib.import_module(parent_name)
            with tempfile.TemporaryDirectory() as temporary:
                source = Path(temporary) / "candidate.py"
                source.write_text(answer)
                spec = importlib.util.spec_from_file_location(module_name, source)
                if spec is None or spec.loader is None:
                    return False
                module = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = module
                setattr(parent, leaf, module)
                spec.loader.exec_module(module)
                import pytest

                target = str(root / rule["test"])
                if rule.get("nodeid"):
                    target += "::" + rule["nodeid"]
                return (
                    pytest.main(["-q", "--tb=short", "-p", "no:cacheprovider", target])
                    == 0
                )
        except (ImportError, AttributeError, SyntaxError, ValueError, TypeError):
            return False
    raise ValueError(f"unknown evaluator mode: {mode}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--case", type=Path, required=True)
    parser.add_argument("--answer", type=Path, required=True)
    args = parser.parse_args()
    rule = json.loads((args.root / args.case).read_text())
    return 0 if evaluate(rule, args.answer.read_text(), args.root) else 1


if __name__ == "__main__":
    raise SystemExit(main())
