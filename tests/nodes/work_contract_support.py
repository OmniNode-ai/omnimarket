"""Lab-runner boundary used by offline controls; no model or broker is called."""

import os
import subprocess
import sys
from pathlib import Path

from omnimarket.handlers.work_contracts.bindings import finish_check, prepare_check
from omnimarket.nodes.node_model_work_contract_compute.handlers.checks import (
    evaluate_schema,
)

ROOT = Path(__file__).parents[2]
CORPUS = ROOT / "src/omnimarket/configs/work_contracts"


def run_control(contract, answer):
    if contract.acceptance.kind == "schema":
        return evaluate_schema(contract.acceptance.spec, answer.read_text())
    argv = prepare_check(contract, ROOT, answer)
    # The current project's locked interpreter is the lab binding, not a model-selected executable.
    argv = (sys.executable, *argv[1:])
    env = {
        k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "PYTEST_ADDOPTS"}
    }
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONPATH"] = str(Path(__file__).parents[2] / "src")
    try:
        result = subprocess.run(
            argv,
            cwd=ROOT,
            env=env,
            capture_output=True,
            timeout=contract.budgets.max_wall_s,
        )
    except subprocess.TimeoutExpired:
        return finish_check(contract, ROOT, exit_code=1, timed_out=True).passed
    if result.returncode not in (0, 1):
        raise AssertionError(
            f"{contract.contract_id}: evaluator failure {result.returncode}: {result.stderr.decode()[-2000:]} {result.stdout.decode()[-1000:]}"
        )
    return finish_check(contract, ROOT, exit_code=result.returncode).passed
