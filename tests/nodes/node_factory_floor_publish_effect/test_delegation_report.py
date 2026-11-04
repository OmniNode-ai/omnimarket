# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Projection-backed delegation windows, including failures and missing reads."""

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import get_type_hints

import pytest

from omnimarket.nodes.node_factory_floor_publish_effect.handlers.handler_delegation_report import (
    CONFIG_PY,
    HandlerDelegationReport,
    delegation_report,
    tick_line,
)
from omnimarket.nodes.node_factory_floor_publish_effect.models.model_delegation_report import (
    ModelDelegationReadOverlay,
    ModelDelegationReportRequest,
)

pytestmark = pytest.mark.unit
NOW = datetime(2026, 11, 4, 13, tzinfo=UTC)


def event(run: str, hours: float, attempts: list[dict], **fields: object) -> dict:
    return {
        "correlation_id": run,
        "timestamp": (NOW - timedelta(hours=hours)).isoformat(),
        "attempt_history": attempts,
        "data_source": "real",
        **fields,
    }


def attempt(backend: str, **fields: object) -> dict:
    return {
        "backend_id": backend,
        "tier": "local" if backend.startswith("local-") else "harness",
        "model_id": "qwen" if backend.startswith("local-") else "gpt-model",
        "host": "gpu-a" if backend.startswith("local-") else "worker",
        "acceptance_decision": "accept",
        **fields,
    }


def report(events: list[dict], **fields: object) -> dict:
    return delegation_report(
        {
            "now": NOW.isoformat(),
            "total_rows": 100,
            "latest_local": "2026-11-01T00:00:00Z",
            "rows": events,
            **fields,
        },
        [],
    )


def test_windows_count_runs_once_per_rung_and_keep_attempt_outcomes() -> None:
    rows = [
        event(
            "a",
            1,
            [
                attempt("local-coder", acceptance_decision="reject"),
                attempt("local-coder"),
                attempt("harness:codex:gpt"),
            ],
        ),
        event(
            "b",
            3,
            [
                attempt(
                    "local-coder",
                    host="gpu-b",
                    acceptance_decision=None,
                    failure_class="timeout",
                )
            ],
        ),
        event("c", 24, [attempt("local-coder")]),
        event("d", -1, [attempt("local-coder")]),
        event("seed", 1, [attempt("local-coder")], data_source="seeded"),
    ]
    result = report(rows)
    short, day = result["windows"]["2h"], result["windows"]["24h"]
    assert (short["total_runs"], short["local_runs"]) == (1, 1)
    assert (day["total_runs"], day["local_runs"]) == (2, 2)
    local = next(r for r in short["rungs"] if r["rung"] == "local")
    assert (
        local["runs"],
        local["attempts"],
        local["accepted"],
        local["refused"],
        local["failed"],
    ) == (1, 2, 1, 1, 0)
    failed = next(r for r in day["rungs"] if r["host"] == "gpu-b")
    assert failed["failed"] == 1


def test_no_history_is_an_unattributed_run_not_a_local_attempt() -> None:
    result = report(
        [event("a", 1, [], model_name="remote-model", quality_gate_passed=False)]
    )
    assert result["windows"]["2h"]["total_runs"] == 1
    assert result["windows"]["2h"]["local_runs"] == 0
    assert result["windows"]["2h"]["unattributed_runs"] == 1
    line = tick_line(result, lanes_running=3)
    assert line.startswith("MISSING delegation-local 2h local=0/1 delegated lanes=3")
    assert "FIX:" in line
    assert tick_line(result, lanes_running=0).startswith("MISSING")


def test_zero_has_positive_control_and_unread_never_becomes_zero() -> None:
    assert tick_line(report([]), lanes_running=0).startswith(
        "OK delegation-local 2h local=0/0"
    )
    assert tick_line(report([]), lanes_running=2).startswith("MISSING")
    with pytest.raises(ValueError, match="positive control"):
        report([], total_rows=0)
    with pytest.raises(ValueError, match="rows"):
        report([], rows=None)
    assert tick_line(
        {"status": "unread", "reason": "read failed"}, lanes_running=2
    ).startswith("UNREAD")


def test_local_inventory_keeps_idle_hosts_and_does_not_assert_endpoints_down() -> None:
    result = delegation_report(
        {
            "now": NOW.isoformat(),
            "total_rows": 2,
            "rows": [],
            "latest_local": "2026-11-01T00:00:00Z",
        },
        [
            {"backend_id": "local-a", "model_name": "qwen", "endpoint_url": None},
            {
                "backend_id": "local-b",
                "model_name": "qwen",
                "endpoint_url": "http://gpu-b/v1/chat/completions",
            },
        ],
    )
    assert result["latest_local"] == "2026-11-01T00:00:00Z"
    assert "unbound" in result["cause"]
    assert "down" not in result["cause"]
    assert len(result["endpoints"]) == 2


def test_harness_acceptance_and_distinct_provider_categories() -> None:
    result = report(
        [
            event(
                "glm",
                1,
                [
                    attempt(
                        "harness:claude-glm:glm-5.3",
                        model_id="glm-5.3",
                        acceptance_decision=None,
                        quality_gate_passed=True,
                    )
                ],
            ),
            event("codex", 1, [attempt("harness:codex:gpt")]),
            event("other", 1, [attempt("remote", tier="cheap_frontier")]),
        ]
    )
    rows = result["windows"]["2h"]["rungs"]
    assert {r["rung"] for r in rows} == {"GLM", "Codex", "other"}
    assert next(r for r in rows if r["rung"] == "GLM")["accepted"] == 1


def test_terminal_outcome_without_history_does_not_invent_attempts() -> None:
    result = report(
        [
            event(
                "a",
                1,
                [],
                operational_outcome="quality_rejected",
                terminal_ok=False,
                quality_gate_passed=False,
            )
        ]
    )
    row = result["windows"]["2h"]["rungs"][0]
    assert row["refused_runs"] == 1
    assert row["attempts"] == 0
    assert result["windows"]["2h"]["by_rung"]["local"]["runs"] == 0


def test_page_renders_zero_local_missing_history_and_escaped_model_names() -> None:
    page = (
        Path(__file__).resolve().parents[3]
        / "src/omnimarket/nodes/node_factory_floor_publish_effect/page/index.html"
    ).read_text()
    start = page.index("function delegationCard(")
    function = page[start : page.index("\nfunction ", start + 1)]
    escape = next(line for line in page.splitlines() if line.startswith("const esc ="))
    data = report([event("a", 1, [], model_name="<script>bad</script>")])
    program = (
        escape
        + "\nconst clock = x => x; const ageMs = x => 0; const STALE_MIN = 15;\n"
        + function
    )
    program += f"\nconsole.log(delegationCard({json.dumps(data)}));"
    result = subprocess.run(
        ["node", "-e", program], capture_output=True, text=True, check=True
    )
    assert "0 local / 1 delegated runs" in result.stdout
    assert "&lt;script&gt;bad&lt;/script&gt;" in result.stdout
    assert "<script>bad</script>" not in result.stdout
    assert "1 runs have no attempt history" in result.stdout


def test_tick_entry_requires_live_lane_count_and_never_publishes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from omnimarket.nodes.node_factory_floor_publish_effect import (
        runtime_factory_floor_publish as runtime,
    )
    from omnimarket.nodes.node_factory_floor_publish_effect.models.model_delegation_report import (
        ModelDelegationReportResult,
    )

    with pytest.raises(SystemExit):
        runtime.parse_args(["--delegation-only", "--tick"])
    data = report([])
    monkeypatch.setattr(
        runtime.HandlerDelegationReport,
        "handle",
        lambda _self, request: ModelDelegationReportResult(
            report=data, tick=tick_line(data, lanes_running=request.lanes_running)
        ),
    )

    def refuse_publish(*args: object, **kwargs: object) -> None:
        pytest.fail("a read must never publish")

    monkeypatch.setattr(runtime.HandlerOperatorAccess, "handle", refuse_publish)
    assert runtime.main(["--delegation-only", "--tick", "--lanes-running", "2"]) == 0
    assert capsys.readouterr().out.startswith(
        "MISSING delegation-local 2h local=0/0 delegated lanes=2"
    )


def test_effect_reads_once_and_keeps_remote_failure_unread() -> None:
    overlay = ModelDelegationReadOverlay(
        ssh_host_env="LAB_SSH",
        compose_project="lab",
        postgres_service="postgres",
        runtime_service="runtime",
        database="projection",
        db_user="reader",
        contract_path="/app/delegation.yaml",
    )
    calls = []

    def run(
        argv: list[str], stdin: bytes | None, timeout: int
    ) -> subprocess.CompletedProcess[bytes]:
        calls.append((argv, stdin, timeout))
        payload = json.dumps({"now": NOW.isoformat(), "total_rows": 4, "rows": []})
        return subprocess.CompletedProcess(
            argv,
            0,
            (payload + '\n@@CONFIG\n{"endpoints": [], "chains": []}').encode(),
            b"",
        )

    request = ModelDelegationReportRequest(overlay=overlay, lanes_running=1)
    result = HandlerDelegationReport(run).handle(
        request, environ={"LAB_SSH": "lab-host"}
    )
    assert result.report["status"] == "read"
    assert len(calls) == 1
    assert b"BEGIN READ ONLY" in (calls[0][1] or b"")
    assert b"e.data_source = 'real'" in (calls[0][1] or b"")
    compile(CONFIG_PY, "remote-config", "exec")

    def failed(
        argv: list[str], stdin: bytes | None, timeout: int
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(argv, 1, b"", b"remote failed")

    unread = HandlerDelegationReport(failed).handle(
        request, environ={"LAB_SSH": "lab-host"}
    )
    assert unread.report["status"] == "unread"
    assert "windows" not in unread.report
    assert unread.tick.startswith("UNREAD")


def test_report_runner_annotations_resolve_for_runtime_introspection() -> None:
    hints = get_type_hints(HandlerDelegationReport.__init__)
    assert "runner" in hints
    assert hints["return"] is type(None)
    result = HandlerDelegationReport().handle(
        ModelDelegationReportRequest(), environ={}
    )
    assert result.report["status"] == "unread"


@pytest.mark.parametrize(
    "failure",
    [OSError("ssh unavailable"), subprocess.TimeoutExpired("ssh", 120)],
)
def test_report_process_boundary_failure_is_an_unread_result(
    monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    overlay = ModelDelegationReadOverlay(
        ssh_host_env="LAB_SSH",
        compose_project="lab",
        postgres_service="postgres",
        runtime_service="runtime",
        database="projection",
        db_user="reader",
        contract_path="/app/delegation.yaml",
    )

    def fail(*args: object, **kwargs: object) -> None:
        raise failure

    monkeypatch.setattr(subprocess, "run", fail)
    result = HandlerDelegationReport().handle(
        ModelDelegationReportRequest(overlay=overlay),
        environ={"LAB_SSH": "lab-host"},
    )
    assert result.report["status"] == "unread"
    assert "windows" not in result.report
    assert result.tick.startswith("UNREAD")


def test_report_groups_follow_the_contract_policy() -> None:
    from omnimarket.nodes.node_factory_floor_publish_effect.models.model_delegation_report import (
        ModelDelegationReportPolicy,
    )

    policy = ModelDelegationReportPolicy.model_validate(
        {
            "local_group": "on-host",
            "fallback_group": "unclassified",
            "groups": [
                {"name": "on-host", "tiers": ["machine"], "backend_prefixes": ["gpu:"]},
                {"name": "review", "model_prefixes": ["review-"]},
            ],
        }
    )
    result = delegation_report(
        {
            "now": NOW.isoformat(),
            "total_rows": 4,
            "rows": [
                event("a", 1, [attempt("worker", tier="machine")]),
                event("b", 1, [attempt("gpu:worker")]),
                event("c", 1, [attempt("worker", model_id="REVIEW-model")]),
                event("d", 1, [attempt("local-old")]),
            ],
        },
        [],
        policy=policy,
    )
    window = result["windows"]["2h"]
    assert window["local_runs"] == 2
    assert {name: values["runs"] for name, values in window["by_rung"].items()} == {
        "on-host": 2,
        "review": 1,
        "unclassified": 1,
    }


@pytest.mark.parametrize("status", [None, "read", "unread", "invalid"])
def test_page_freshness_accepts_legacy_counts_but_keeps_unread_reports(
    status: str | None,
) -> None:
    page = (
        Path(__file__).resolve().parents[3]
        / "src/omnimarket/nodes/node_factory_floor_publish_effect/page/index.html"
    ).read_text()
    start = page.index("function freshRows(")
    function = page[start : page.index("\nfunction ", start + 1)]
    data = {"generated_at": NOW.isoformat(), "reason": "projection unavailable"}
    if status is not None:
        data["status"] = status
    program = "const num = x => Number(x) || 0;\n" + function
    program += (
        f"\nconsole.log(JSON.stringify(freshRows({{sources: {{}}}}, null, null, null, "
        f"{json.dumps(data)}).find(r => r.name === 'Delegation counts')));"
    )
    result = subprocess.run(
        ["node", "-e", program], capture_output=True, text=True, check=True
    )
    row = json.loads(result.stdout)
    if status in {None, "read"}:
        assert row["iso"] == data["generated_at"]
        assert not row.get("flag")
    else:
        assert row["flag"] is True
        assert row["ok"] is False
        assert row["note"] == data["reason"]
