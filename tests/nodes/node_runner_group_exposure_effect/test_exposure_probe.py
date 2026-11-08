# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_runner_group_exposure_effect: golden chain, error chain and named failing controls, proven on
both overlays (the in-memory source and the GitHub source against a local HTTP fixture) (OMN-22860)."""

from __future__ import annotations

import base64
import json
import logging
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.models.model_reading import EnumReadingVerdict
from omnimarket.models.runner_group_exposure import (
    EnumExposureReadFailure,
)
from omnimarket.nodes.node_runner_group_exposure_compute.handlers.handler_runner_group_exposure import (
    SUBJECT_ALLOWS_PUBLIC,
    SUBJECT_PINNED_EQUALS_FILES,
    SUBJECT_PUBLIC_JOBS_WINDOW,
    SUBJECT_PUBLIC_REACH_FILES,
    SUBJECT_READ_AGE,
    SUBJECTS,
)
from omnimarket.nodes.node_runner_group_exposure_effect.handlers.handler_exposure_probe import (
    HandlerExposureProbe,
    load_topics,
)
from omnimarket.nodes.node_runner_group_exposure_effect.models.model_exposure_probe import (
    ModelExposureProbeRequest,
    ModelExposureProbeResult,
)
from omnimarket.nodes.node_runner_group_exposure_effect.overlay import (
    NODE_NAME,
    load_overlay,
)
from tests.node_overlay_support import install_node_overlay

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
NODE_DIR = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_runner_group_exposure_effect"
)
TOKEN_ENV = "EXAMPLE_GROUP_READ_TOKEN"
TOKEN_VALUE = "example-token-value-0123456789"
REPO = "example-org/example-repo"
PATH = ".github/workflows/build.yml"
IDENTITY = f"{REPO}/{PATH}@main"
SETTINGS = {
    "public_reach_events": ["pull_request_target", "pull_request", "issue_comment"],
    "max_age_seconds": 3600,
}
BACKENDS = ("inmemory", "github")


@dataclass
class Scenario:
    """The facts of one group, served by either backend; the defaults are a clean group."""

    allows_public: bool = False
    restricted: bool = True
    selected: list[str] = field(default_factory=lambda: [IDENTITY])
    triggers: list[str] = field(default_factory=lambda: ["push", "workflow_dispatch"])
    job_events: list[str] = field(default_factory=lambda: ["push"])
    group_status: int = 200
    listing_status: int = 200
    read_age_seconds: int = 60
    read_failure: str | None = None


def _workflow_text(triggers: list[str]) -> str:
    on = "\n".join(f"  {t}: {{}}" for t in triggers)
    return (
        f"on:\n{on}\njobs:\n  build:\n    runs-on: [example-private-group]\n"
        "    steps:\n      - run: echo ok\n"
    )


class _Server(ThreadingHTTPServer):
    scenario: Scenario
    seen_auth: list[str | None]


class _Handler(BaseHTTPRequestHandler):
    server: _Server

    def log_message(self, format: str, *args: object) -> None:
        return

    def _send(self, status: int, payload: object) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        s = self.server.scenario
        self.server.seen_auth.append(self.headers.get("Authorization"))
        path = self.path
        if path == "/orgs/example-org/actions/runner-groups/7":
            if s.group_status != 200:
                return self._send(s.group_status, {"message": "refused"})
            return self._send(
                200,
                {
                    "allows_public_repositories": s.allows_public,
                    "restricted_to_selected_workflows": s.restricted,
                    "selected_workflows": s.selected,
                },
            )
        if path == "/orgs/example-org/actions/runner-groups/7/repositories":
            return self._send(200, {"repositories": [{"full_name": REPO}]})
        if path == f"/repos/{REPO}":
            return self._send(200, {"default_branch": "main"})
        if path == f"/repos/{REPO}/contents/.github/workflows?ref=main":
            if s.listing_status != 200:
                return self._send(s.listing_status, {"message": "broken"})
            return self._send(200, [{"path": PATH, "type": "file"}])
        if path == f"/repos/{REPO}/contents/{PATH}?ref=main":
            text = _workflow_text(s.triggers)
            return self._send(
                200, {"content": base64.b64encode(text.encode()).decode()}
            )
        if path == f"/repos/{REPO}/actions/runs?status=queued":
            runs = [
                {
                    "id": 100 + i,
                    "event": event,
                    "path": PATH,
                    "repository": {"full_name": REPO, "private": True},
                    "head_repository": {"full_name": REPO},
                }
                for i, event in enumerate(s.job_events)
            ]
            return self._send(200, {"workflow_runs": runs})
        if path == f"/repos/{REPO}/actions/runs?status=in_progress":
            return self._send(200, {"workflow_runs": []})
        return self._send(404, {"message": "not found"})


@pytest.fixture
def server() -> Iterator[_Server]:
    httpd = _Server(("127.0.0.1", 0), _Handler)
    httpd.scenario = Scenario()
    httpd.seen_auth = []
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd
    httpd.shutdown()
    httpd.server_close()


def _fixture(s: Scenario) -> dict[str, object]:
    return {
        "read_age_seconds": s.read_age_seconds,
        "read_failure": s.read_failure,
        "group": None
        if s.read_failure
        else {
            "group_id": 7,
            "allows_public_repositories": s.allows_public,
            "restricted_to_workflows": s.restricted,
            "selected_workflows": s.selected,
            "repositories": [REPO],
        },
        "files": []
        if s.read_failure
        else [
            {
                "repository": REPO,
                "path": PATH,
                "ref": "main",
                "selects_group_label": True,
                "triggers": s.triggers,
            }
        ],
        "jobs": [
            {
                "run_id": 100 + i,
                "run_event": event,
                "head_is_fork": False,
                "workflow_ref": PATH,
                "repository_private": True,
            }
            for i, event in enumerate(s.job_events)
        ],
    }


def _probe(
    backend: str,
    scenario: Scenario,
    server: _Server,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    max_calls: int = 60,
) -> HandlerExposureProbe:
    """Install the overlay of ``backend`` for ``scenario`` and return the handler that reads it."""
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    monkeypatch.setenv(TOKEN_ENV, TOKEN_VALUE)
    server.scenario = scenario
    if backend == "inmemory":
        source: dict[str, object] = {
            "backend": "inmemory",
            "fixture": _fixture(scenario),
        }
    else:
        source = {
            "backend": "github",
            "api_base": f"http://127.0.0.1:{server.server_address[1]}",
            "org": "example-org",
            "group_id": 7,
            "group_labels": ["example-private-group"],
            "token_env": TOKEN_ENV,
            "max_calls": max_calls,
            "timeout_seconds": 5,
        }
    install_node_overlay(
        monkeypatch, tmp_path, NODE_NAME, {"settings": SETTINGS, "source": source}
    )
    return HandlerExposureProbe()


def _run(handler: HandlerExposureProbe) -> ModelExposureProbeResult:
    return handler.handle(ModelExposureProbeRequest(correlation_id="probe-1", now=NOW))


def _verdicts(result: ModelExposureProbeResult) -> dict[str, EnumReadingVerdict]:
    return {s.name: s.verdict for s in result.reading.subjects}


# -- golden chain --------------------------------------------------------------------------


@pytest.mark.parametrize("backend", BACKENDS)
def test_golden_chain_clean_group_reads_working(
    backend: str, server: _Server, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _run(_probe(backend, Scenario(), server, tmp_path, monkeypatch))
    assert result.backend == backend
    assert result.reading.verdict is EnumReadingVerdict.WORKING
    assert tuple(s.name for s in result.reading.subjects) == SUBJECTS
    assert set(_verdicts(result).values()) == {EnumReadingVerdict.WORKING}
    topic, payload = HandlerExposureProbe.event_for(result)
    assert topic == load_topics()["probe_completed"]
    assert payload["reading"]["verdict"] == "WORKING"
    if backend == "github":
        assert result.calls > 0
        assert set(server.seen_auth) == {f"Bearer {TOKEN_VALUE}"}


# -- named failing controls: each break reads NOT_WORKING on its subject, on both overlays ----


BREAKS = {
    "a_public_trigger_on_a_group_file": (
        Scenario(triggers=["push", "pull_request_target"]),
        SUBJECT_PUBLIC_REACH_FILES,
    ),
    "b_group_not_restricted_to_workflows": (
        Scenario(restricted=False),
        SUBJECT_PINNED_EQUALS_FILES,
    ),
    "c_group_allows_public_repositories": (
        Scenario(allows_public=True),
        SUBJECT_ALLOWS_PUBLIC,
    ),
    "d_public_origin_job_on_the_group": (
        Scenario(job_events=["push", "pull_request_target"]),
        SUBJECT_PUBLIC_JOBS_WINDOW,
    ),
}


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("name", sorted(BREAKS))
def test_injected_break_reads_not_working_on_its_subject(
    name: str,
    backend: str,
    server: _Server,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario, subject = BREAKS[name]
    result = _run(_probe(backend, scenario, server, tmp_path, monkeypatch))
    assert result.reading.verdict is EnumReadingVerdict.NOT_WORKING
    assert _verdicts(result)[subject] is EnumReadingVerdict.NOT_WORKING
    # the positive control: the same overlay without the break reads WORKING
    clean = _run(_probe(backend, Scenario(), server, tmp_path, monkeypatch))
    assert clean.reading.verdict is EnumReadingVerdict.WORKING


# -- error chain ---------------------------------------------------------------------------


def test_error_chain_org_read_denied_reads_unknown_in_memory(
    server: _Server, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = Scenario(read_failure="org-read-denied")
    result = _run(_probe("inmemory", scenario, server, tmp_path, monkeypatch))
    _assert_unknown(result, EnumExposureReadFailure.ORG_READ_DENIED)


def test_error_chain_org_read_denied_reads_unknown_over_http(
    server: _Server, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = Scenario(group_status=403)
    result = _run(_probe("github", scenario, server, tmp_path, monkeypatch))
    _assert_unknown(result, EnumExposureReadFailure.ORG_READ_DENIED)


def test_error_chain_a_broken_workflow_listing_reads_unreadable(
    server: _Server, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = Scenario(listing_status=500)
    result = _run(_probe("github", scenario, server, tmp_path, monkeypatch))
    _assert_unknown(result, EnumExposureReadFailure.UNREADABLE)


def test_error_chain_the_call_past_the_budget_is_not_made(
    server: _Server, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler = _probe("github", Scenario(), server, tmp_path, monkeypatch, max_calls=2)
    result = _run(handler)
    _assert_unknown(result, EnumExposureReadFailure.BUDGET_EXHAUSTED)
    assert result.calls == 2


def test_error_chain_a_missing_token_reads_denied_and_makes_no_call(
    server: _Server, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler = _probe("github", Scenario(), server, tmp_path, monkeypatch)
    monkeypatch.delenv(TOKEN_ENV)
    result = _run(handler)
    _assert_unknown(result, EnumExposureReadFailure.ORG_READ_DENIED)
    assert result.calls == 0
    assert server.seen_auth == []


def test_error_chain_a_stale_read_reads_unknown(
    server: _Server, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = Scenario(read_age_seconds=7200)
    result = _run(_probe("inmemory", scenario, server, tmp_path, monkeypatch))
    assert result.reading.verdict is EnumReadingVerdict.UNKNOWN
    assert _verdicts(result)[SUBJECT_READ_AGE] is EnumReadingVerdict.UNKNOWN
    topic, _ = HandlerExposureProbe.event_for(result)
    assert topic == load_topics()["probe_completed"]


def _assert_unknown(
    result: ModelExposureProbeResult, failure: EnumExposureReadFailure
) -> None:
    assert result.reading.verdict is EnumReadingVerdict.UNKNOWN
    assert result.reading.read_failure is failure
    assert set(_verdicts(result).values()) == {EnumReadingVerdict.UNKNOWN}
    topic, payload = HandlerExposureProbe.event_for(result)
    assert topic == load_topics()["probe_failed"]
    assert payload["reading"]["read_failure"] == failure.value


def test_the_token_value_is_in_no_result_and_no_log(
    server: _Server,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    handler = _probe(
        "github", Scenario(group_status=403), server, tmp_path, monkeypatch
    )
    result = _run(handler)
    assert TOKEN_VALUE not in result.model_dump_json()
    assert TOKEN_VALUE not in caplog.text


# -- overlay and contract ----------------------------------------------------------------------


def test_shipped_examples_load_and_the_in_memory_one_reads_working(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    memory = yaml.safe_load((NODE_DIR / "overlay.example.yaml").read_text())
    install_node_overlay(monkeypatch, tmp_path, NODE_NAME, memory)
    assert load_overlay().source.backend == "inmemory"
    assert _run(HandlerExposureProbe()).reading.verdict is EnumReadingVerdict.WORKING
    real = yaml.safe_load((NODE_DIR / "overlay.github.example.yaml").read_text())
    install_node_overlay(monkeypatch, tmp_path, NODE_NAME, real)
    assert load_overlay().source.backend == "github"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.pop("settings"),
        lambda d: d["settings"].pop("max_age_seconds"),
        lambda d: d["source"].pop("backend"),
        lambda d: d["source"].pop("fixture"),
    ],
)
def test_an_incomplete_in_memory_overlay_refuses(
    mutate: Callable[[dict[str, Any]], object],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    document = yaml.safe_load((NODE_DIR / "overlay.example.yaml").read_text())
    mutate(document)
    install_node_overlay(monkeypatch, tmp_path, NODE_NAME, document)
    with pytest.raises(NodeOverlayError):
        load_overlay()


@pytest.mark.parametrize("key", ["token_env", "max_calls", "api_base"])
def test_a_github_overlay_without_a_key_refuses_and_names_it(
    key: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    document = yaml.safe_load((NODE_DIR / "overlay.github.example.yaml").read_text())
    document["source"].pop(key)
    install_node_overlay(monkeypatch, tmp_path, NODE_NAME, document)
    with pytest.raises(NodeOverlayError, match=key):
        load_overlay()


def test_contract_declares_the_probe_topics() -> None:
    topics = load_topics()
    assert (
        topics["probe_requested"]
        == "onex.cmd.omnimarket.runner-group-exposure-probe-requested.v1"
    )
    assert (
        topics["probe_completed"]
        == "onex.evt.omnimarket.runner-group-exposure-probed.v1"
    )
    assert (
        topics["probe_failed"]
        == "onex.evt.omnimarket.runner-group-exposure-probe-failed.v1"
    )
    assert topics["probe_requested"].startswith("onex.cmd.omnimarket.")
    assert topics["probe_completed"].startswith("onex.evt.omnimarket.")
    assert topics["probe_failed"].startswith("onex.evt.omnimarket.")
