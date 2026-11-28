# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The composed-runtime boot check: a /health bound, a loop-hold bound, and the call a stack sampler names.

The four named falsifiers are ``boot_health_bound``, ``sync_wire_from_manifest_regression``,
``loop_hold_over_one_second`` and ``startup_hold_2026_11_21``. Each runs the node's own handlers: the sampler runs on a
real asyncio loop, the probe talks to a real local HTTP server, and the verdict is the handler's.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_composed_boot_loop_hold_check.adapters.adapter_boot_limits import (
    LimitSourceError,
    limits_applied_error,
    memory_bytes,
    render_override,
    resolve_bounds,
    stage_sampler,
)
from omnimarket.nodes.node_composed_boot_loop_hold_check.adapters.adapter_boot_observe import (
    HttpProbe,
    parse_hold_records,
    probe_until_ready,
)
from omnimarket.nodes.node_composed_boot_loop_hold_check.handlers.handler_boot_check import (
    HandlerBootCheck,
)
from omnimarket.nodes.node_composed_boot_loop_hold_check.models.model_boot_bounds import (
    ModelBootBounds,
)
from omnimarket.nodes.node_composed_boot_loop_hold_check.models.model_boot_check_request import (
    ModelBootCheckRequest,
)
from omnimarket.nodes.node_composed_boot_loop_hold_check.models.model_boot_check_result import (
    EnumBootFinding,
    ModelBootCheckResult,
)
from omnimarket.nodes.node_composed_boot_loop_hold_check.models.model_boot_observation import (
    ModelBootObservation,
    ModelHealthSample,
    ModelLoopHold,
)
from omnimarket.nodes.node_composed_boot_loop_hold_check.sampler.loop_hold_sampler import (
    LoopHoldSampler,
)

NODE_DIR = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_composed_boot_loop_hold_check"
)
FIXTURE = Path(__file__).parent / "fixtures" / "startup_hold_2026_11_21.json"
DECLARED = yaml.safe_load((NODE_DIR / "contract.yaml").read_text(encoding="utf-8"))[
    "boot_check"
]
LOOP_HOLD_BOUND = float(DECLARED["loop_hold_bound_seconds"])
ANSWERED_AT_ONCE = ModelHealthSample(
    offset_seconds=0.5, answered=True, latency_seconds=0.05
)


def _bounds(health_bound_seconds: float = 120.0) -> ModelBootBounds:
    return ModelBootBounds(
        loop_hold_bound_seconds=LOOP_HOLD_BOUND,
        health_bound_seconds=health_bound_seconds,
        cpus=1.0,
        memory_limit="3072M",
    )


def _judge(
    holds: tuple[ModelLoopHold, ...],
    samples: tuple[ModelHealthSample, ...] = (ANSWERED_AT_ONCE,),
    health_bound_seconds: float = 120.0,
) -> ModelBootCheckResult:
    return HandlerBootCheck().handle(
        ModelBootCheckRequest(
            bounds=_bounds(health_bound_seconds),
            observation=ModelBootObservation(health_samples=samples, loop_holds=holds),
        )
    )


async def _as_coroutine(bootstrap: Callable[[], Awaitable[None]]) -> None:
    await bootstrap()


def _boot_under_sampler(
    bootstrap: Callable[[], Awaitable[None]],
) -> tuple[ModelLoopHold, ...]:
    """Run a bootstrap coroutine on a real loop with the sampler installed; return the holds it recorded."""
    records: list[dict[str, Any]] = []
    sampler = LoopHoldSampler(LOOP_HOLD_BOUND, 0.02, records.append)
    sampler.install()
    try:
        asyncio.run(_as_coroutine(bootstrap))
    finally:
        sampler.uninstall()
    return parse_hold_records("\n".join(json.dumps(record) for record in records))


# --- health bound (AC1) -------------------------------------------------------------------------------------------


@contextmanager
def _health_server(
    ready_after_seconds: float, body: dict[str, Any], status: int = 200
) -> Iterator[str]:
    ready_at = time.monotonic() + ready_after_seconds

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            wait = ready_at - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            payload = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: Any) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/health"
    finally:
        server.shutdown()
        server.server_close()


HEALTHY = {"status": "healthy", "details": {"is_running": True}}


def _probe(url: str) -> tuple[ModelHealthSample, ...]:
    return probe_until_ready(
        HttpProbe(url),
        window_seconds=5.0,
        probe_timeout_seconds=0.3,
        interval_seconds=0.1,
        elapsed_at_start=0.0,
    )


def test_boot_health_bound_goes_red_when_health_is_delayed_past_the_bound() -> None:
    with _health_server(ready_after_seconds=2.0, body=HEALTHY) as url:
        samples = _probe(url)
    assert any(not sample.answered for sample in samples), (
        "the probe must have met the delay"
    )
    result = _judge((), samples, health_bound_seconds=1.0)
    assert not result.green
    assert [f.kind for f in result.findings] == [
        EnumBootFinding.HEALTH_NOT_ANSWERED_WITHIN_BOUND
    ]
    assert result.first_answer_seconds is not None
    assert result.first_answer_seconds > 1.0


def test_boot_health_bound_stays_green_when_health_answers_inside_the_bound() -> None:
    with _health_server(ready_after_seconds=0.0, body=HEALTHY) as url:
        samples = _probe(url)
    result = _judge((), samples, health_bound_seconds=1.0)
    assert result.green, result.findings
    assert result.first_answer_seconds is not None
    assert result.first_answer_seconds < 1.0


def test_boot_health_bound_a_503_body_is_an_answer() -> None:
    degraded = {
        "status": "degraded",
        "details": {"degraded_reason": "event_loop_unresponsive"},
    }
    with _health_server(ready_after_seconds=0.0, body=degraded, status=503) as url:
        body = HttpProbe(url).fetch(1.0)
    assert body is not None
    assert "event_loop_unresponsive" in body


def test_boot_health_bound_no_answer_at_all_is_red() -> None:
    result = _judge(
        (),
        (ModelHealthSample(offset_seconds=0.0, answered=False, latency_seconds=5.0),),
    )
    assert not result.green
    assert result.first_answer_seconds is None


# --- a synchronous wire_from_manifest in the bootstrap coroutine (AC2) --------------------------------------------

ITEMS = tuple(range(4))
ITEM_SECONDS = 0.4


def _prepare_contract_wiring(_item: int) -> None:
    time.sleep(ITEM_SECONDS)


def wire_from_manifest_sync(items: tuple[int, ...]) -> None:
    for item in items:
        _prepare_contract_wiring(item)


async def wire_from_manifest_yielding(items: tuple[int, ...]) -> None:
    for item in items:
        _prepare_contract_wiring(item)
        await asyncio.sleep(0)


async def _bootstrap_with_the_regression() -> None:
    wire_from_manifest_sync(ITEMS)


async def _bootstrap_with_the_fix() -> None:
    await wire_from_manifest_yielding(ITEMS)


def test_sync_wire_from_manifest_regression_turns_the_check_red() -> None:
    holds = _boot_under_sampler(_bootstrap_with_the_regression)
    result = _judge(holds)
    assert not result.green
    assert result.findings[0].kind is EnumBootFinding.LOOP_HOLD_OVER_BOUND
    assert result.longest_hold_seconds == pytest.approx(
        len(ITEMS) * ITEM_SECONDS, abs=0.3
    )
    assert any("wire_from_manifest_sync" in frame for frame in holds[0].stack)


def test_sync_wire_from_manifest_regression_check_is_green_with_the_loop_handed_back() -> (
    None
):
    holds = _boot_under_sampler(_bootstrap_with_the_fix)
    result = _judge(holds)
    assert result.green, result.findings
    assert holds == ()


# --- any call over the loop-hold bound, named by the sampler (AC3) ------------------------------------------------


def blocked_for_one_and_a_half_seconds() -> None:
    time.sleep(1.5)


def blocked_for_four_tenths_of_a_second() -> None:
    time.sleep(0.4)


async def _bootstrap_sleeping_one_and_a_half_seconds() -> None:
    blocked_for_one_and_a_half_seconds()


async def _bootstrap_sleeping_four_tenths() -> None:
    blocked_for_four_tenths_of_a_second()


def test_loop_hold_over_one_second_is_named_by_the_sampler_and_red() -> None:
    assert LOOP_HOLD_BOUND == 1.0
    holds = _boot_under_sampler(_bootstrap_sleeping_one_and_a_half_seconds)
    assert len(holds) == 1
    assert "blocked_for_one_and_a_half_seconds" in holds[0].call
    assert holds[0].held_seconds == pytest.approx(1.5, abs=0.25)
    result = _judge(holds)
    assert not result.green
    finding = result.findings[0]
    assert finding.kind is EnumBootFinding.LOOP_HOLD_OVER_BOUND
    assert "blocked_for_one_and_a_half_seconds" in finding.call


def test_loop_hold_over_one_second_a_shorter_call_is_green() -> None:
    holds = _boot_under_sampler(_bootstrap_sleeping_four_tenths)
    assert holds == ()
    assert _judge(holds).green


def test_loop_hold_over_one_second_a_hold_is_judged_per_call_not_by_the_sum() -> None:
    async def many_short_calls() -> None:
        for _ in range(5):
            blocked_for_four_tenths_of_a_second()
            await asyncio.sleep(0)

    assert _boot_under_sampler(many_short_calls) == ()


# --- the 2026-11-21 startup holds (AC4) ---------------------------------------------------------------------------


def _fixture_observation(name: str) -> ModelBootObservation:
    recorded = json.loads(FIXTURE.read_text(encoding="utf-8"))[name]
    return ModelBootObservation(
        health_samples=tuple(
            ModelHealthSample(**s) for s in recorded["health_samples"]
        ),
        loop_holds=tuple(ModelLoopHold(**h) for h in recorded["loop_holds"]),
    )


def test_startup_hold_2026_11_21_fails_on_the_broken_code() -> None:
    result = HandlerBootCheck().handle(
        ModelBootCheckRequest(
            bounds=_bounds(), observation=_fixture_observation("broken")
        )
    )
    assert not result.green
    kinds = {finding.kind for finding in result.findings}
    assert kinds == {
        EnumBootFinding.LOOP_HOLD_OVER_BOUND,
        EnumBootFinding.HEALTH_NOT_ANSWERED_WITHIN_BOUND,
    }
    held = {finding.seconds for finding in result.findings if finding.call}
    assert held == {12.39, 31.6}
    assert any("wire_from_manifest" in finding.call for finding in result.findings)


def test_startup_hold_2026_11_21_passes_on_the_fix() -> None:
    result = HandlerBootCheck().handle(
        ModelBootCheckRequest(
            bounds=_bounds(), observation=_fixture_observation("fixed")
        )
    )
    assert result.green, result.findings


def test_startup_hold_2026_11_21_the_recorded_hold_covers_the_recorded_per_item_work() -> (
    None
):
    profile = json.loads(FIXTURE.read_text(encoding="utf-8"))["profile"]
    per_item_seconds = (profile["prepare_ms_sum"] + profile["import_ms_sum"]) / 1000.0
    assert (
        profile["wire_from_manifest_contiguous_loop_hold_seconds"] >= per_item_seconds
    )
    assert profile["prepare_items"] == 807


def test_startup_hold_2026_11_21_the_unrecorded_hold_is_marked_unrecorded() -> None:
    holds = json.loads(FIXTURE.read_text(encoding="utf-8"))["broken"]["loop_holds"]
    unrecorded = [hold for hold in holds if not hold["recorded"]]
    assert [hold["held_seconds"] for hold in unrecorded] == [31.6]


# --- limits come from the declarations, never from code -----------------------------------------------------------


def _infra_tree(root: Path, memory: str = "3072M", cpus: str = "'1.0'") -> Path:
    policy = root / DECLARED["limit_sources"]["memory_limit"]["file"]
    policy.parent.mkdir(parents=True)
    policy.write_text(
        "profiles:\n  dev:\n    services:\n      main:\n        boot:\n"
        f"          memory_limit: {memory}\n          ready_bound_seconds: 600\n",
        encoding="utf-8",
    )
    compose = root / DECLARED["limit_sources"]["cpus"]["file"]
    compose.parent.mkdir(parents=True)
    compose.write_text(
        f"x-runtime-base: &runtime-base\n  deploy:\n    resources:\n      limits:\n        cpus: {cpus}\n"
        "        memory: 1536M\n",
        encoding="utf-8",
    )
    return root


def test_limits_are_read_from_the_declarations(tmp_path: Path) -> None:
    bounds = resolve_bounds(
        NODE_DIR / "contract.yaml", _infra_tree(tmp_path, memory="4096M", cpus="'2.0'")
    )
    assert (bounds.memory_limit, bounds.cpus) == ("4096M", 2.0)
    assert bounds.loop_hold_bound_seconds == LOOP_HOLD_BOUND


def test_limits_missing_declaration_is_an_error_not_a_default(tmp_path: Path) -> None:
    with pytest.raises(LimitSourceError, match="memory_limit"):
        resolve_bounds(NODE_DIR / "contract.yaml", _blank_policy(tmp_path))


def _blank_policy(root: Path) -> Path:
    _infra_tree(root)
    (root / DECLARED["limit_sources"]["memory_limit"]["file"]).write_text(
        "profiles: {}\n", encoding="utf-8"
    )
    return root


def test_override_applies_the_limits_and_the_sampler_to_every_named_service() -> None:
    override = render_override(
        _bounds(),
        ("omninode-runtime", "runtime-effects"),
        "/sampler",
        "/app/lib",
    )
    assert set(override["services"]) == {"omninode-runtime", "runtime-effects"}
    for service in override["services"].values():
        assert service["deploy"]["resources"]["limits"] == {
            "cpus": "1.0",
            "memory": "3072M",
        }
        assert service["environment"] == {"PYTHONPATH": "/sampler:/app/lib"}
        assert service["volumes"] == ["/sampler:/sampler:ro"]


def test_staged_sampler_carries_its_settings_in_a_file_not_in_the_environment(
    tmp_path: Path,
) -> None:
    """The sampler's output path and bound travel in a staged config file: no environment setting is read."""
    staged = stage_sampler(
        NODE_DIR / "sampler", tmp_path / "staged", _bounds(), "/tmp/holds.jsonl"
    )
    assert json.loads((staged / "sampler_config.json").read_text(encoding="utf-8")) == {
        "out": "/tmp/holds.jsonl",
        "bound_seconds": LOOP_HOLD_BOUND,
    }
    assert {path.name for path in staged.iterdir()} == {
        "sitecustomize.py",
        "loop_hold_sampler.py",
        "sampler_config.json",
    }
    sources = "".join(
        (NODE_DIR / "sampler" / name).read_text(encoding="utf-8")
        for name in ("sitecustomize.py", "loop_hold_sampler.py")
    )
    assert "environ" not in sources
    assert "getenv" not in sources


def test_staged_sampler_names_a_hold_in_a_fresh_interpreter(tmp_path: Path) -> None:
    """A python process started with the staged directory on its path records the call that held its loop."""
    out = tmp_path / "holds.jsonl"
    staged = stage_sampler(
        NODE_DIR / "sampler", tmp_path / "staged", _bounds(), str(out)
    )
    program = (
        "import asyncio, time\n"
        "async def hold_the_loop_for_one_and_a_half_seconds():\n"
        "    time.sleep(1.5)\n"
        "asyncio.run(hold_the_loop_for_one_and_a_half_seconds())\n"
    )
    subprocess.run(
        [sys.executable, "-c", program],
        env={**os.environ, "PYTHONPATH": str(staged)},
        check=True,
        timeout=60,
    )
    holds = parse_hold_records(out.read_text(encoding="utf-8"))
    assert holds, "the sampler recorded no hold"
    assert any("hold_the_loop_for_one_and_a_half_seconds" in h.call for h in holds)


def test_unreadable_sampler_record_is_an_error() -> None:
    with pytest.raises(ValueError, match="line 1"):
        parse_hold_records("not json\n")


def test_applied_limits_are_read_back_against_the_declared_ones() -> None:
    bounds = _bounds()
    assert memory_bytes("3072M") == 3072 * 1024 * 1024
    assert limits_applied_error(bounds, 1_000_000_000, 3072 * 1024 * 1024) is None
    assert "NanoCpus=2000000000" in str(
        limits_applied_error(bounds, 2_000_000_000, 3072 * 1024 * 1024)
    )
    assert "Memory=0" in str(limits_applied_error(bounds, 1_000_000_000, 0))
    with pytest.raises(LimitSourceError):
        memory_bytes("lots")


def test_the_verdict_is_declared_on_the_terminal_topic_the_node_publishes() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text(encoding="utf-8"))
    terminal = "onex.evt.omnimarket.composed-boot-check-completed.v1"
    assert contract["terminal_event"] == terminal
    assert contract["event_bus"]["publish_topics"] == [terminal]
    assert contract["topics"]["composed_boot_check_completed"] == terminal
