import os
import subprocess
from pathlib import Path


def test_demo_script_runs_dry() -> None:
    script = (
        Path(__file__).resolve().parents[1] / "scripts" / "demo_market_cli_outputs.sh"
    )
    result = subprocess.run(
        ["bash", str(script), "--dry-run-only"],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "DEMO_NO_RECORD": "1"},
    )
    assert result.returncode == 0, result.stderr
    assert "ticket_pipeline" in result.stdout
