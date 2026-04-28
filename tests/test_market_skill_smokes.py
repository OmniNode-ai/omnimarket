"""Market-owned operator node smoke tests.

These are intentionally market-only. They prove the direct omnimarket entrypoints
still run without relying on external wrapper surfaces.
"""

from __future__ import annotations

from omnimarket.market_skill_baseline import iter_market_skill_specs, run_cli_smoke


def test_all_market_skill_cli_smokes_pass() -> None:
    for spec in iter_market_skill_specs():
        result = run_cli_smoke(spec)
        assert result.passed, (
            f"{spec.skill_name} smoke failed: rc={result.returncode} "
            f"summary={result.summary} stderr={result.stderr!r}"
        )
