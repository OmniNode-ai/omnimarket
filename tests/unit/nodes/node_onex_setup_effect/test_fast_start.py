"""OMN-26620: onex starts fast - the TUI module does not load the inference stack.

Failure mode (operator 2026-11-25: "when i type onex why it taking time to start"):
importing the TUI imported the model catalogue, which pulled in the inference and
infrastructure stack, about 1.6 s of a 2.1 s start, before the first screen drew.
"""

from __future__ import annotations

import subprocess
import sys

_PROBE = (
    "import sys, time\n"
    "t = time.perf_counter()\n"
    "import omnimarket.nodes.node_onex_setup_effect.cli_tui\n"
    "print(round(time.perf_counter() - t, 3))\n"
    "print(sorted(m for m in ('omnimarket.inference', 'omnimarket.handlers.model_catalog',"
    " 'omnimarket.handlers.models_cli_output') if m in sys.modules))\n"
)


def test_the_tui_module_loads_without_the_inference_stack() -> None:
    out = subprocess.run(
        [sys.executable, "-c", _PROBE], capture_output=True, text=True, check=True
    ).stdout.splitlines()
    assert out[1] == "[]", out


def test_the_catalogue_still_loads_on_first_use() -> None:
    """The positive control: the lazy loader really reaches the catalogue."""
    from omnimarket.nodes.node_onex_setup_effect import cli_tui

    assert cli_tui._lazy(cli_tui._CATALOG).EMPTY_INDEX is not None
