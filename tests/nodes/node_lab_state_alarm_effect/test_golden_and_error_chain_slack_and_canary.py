# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chains of the Slack alarm channel and the canary publication (OMN-19998; plan figure W23).

Golden: an offline source raises an alarm that goes to the alert channel, is read back from the channel, is marked as a
test when a drill raised it, and the cycle publishes the served page's age and the alarm heartbeat for the hosted canary.
Error: Slack refuses the post (HTTP 200 with ok false), the credential cannot be resolved, the read-back never shows the
message, git cannot push, and a stale or missing status is silent to the canary. No secret value appears in any message.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from omnimarket.models.floor_17a import (
    ModelAlarm,
    ModelAlarmChannelDef,
    ModelAlarmOverlay,
    ModelAlarmState,
    ModelCanaryPublishOverlay,
    ModelCanaryStatus,
    ModelFloorSimpleTickRequest,
    ModelFloorSimpleTickResult,
    ModelLabSourceVerdict,
    ModelLabStateAlarmRequest,
    ModelLabStateAlarmResult,
)
from omnimarket.nodes.node_floor_simple_effect.handlers.emission_sink import (
    InMemoryEmissionSink,
)
from omnimarket.nodes.node_floor_simple_effect.handlers.handler_floor_simple_tick import (
    HandlerFloorSimpleTick,
)
from omnimarket.nodes.node_lab_state_alarm_effect.handlers.canary_publisher import (
    CanaryPublishError,
    GitCanaryPublisher,
)
from omnimarket.nodes.node_lab_state_alarm_effect.handlers.channels import (
    AlarmSendRefusedError,
    InMemoryAlarmChannel,
    SlackAlarmChannel,
    alarm_text,
)
from omnimarket.nodes.node_lab_state_alarm_effect.handlers.handler_lab_state_alarm import (
    HandlerLabStateAlarm,
)
from omnimarket.nodes.node_lab_state_alarm_effect.handlers.slack_api import (
    OperatorEnvCredentials,
    SlackWebApi,
    read_named_key,
)
from omnimarket.nodes.node_lab_state_alarm_effect.runtime_canary import main as canary
from tests.nodes.node_floor_simple_effect.support import NOW, build_home, environ

pytestmark = pytest.mark.unit

SECRET_TOKEN = "xoxb-this-value-must-never-appear"
SECRET_CHANNEL = "C0SECRETCHAN"
T0 = datetime(2026, 11, 5, 15, 0, tzinfo=UTC)
OVERLAY = ModelAlarmOverlay(
    readback_s=300,
    renotify_s=3600,
    heartbeat_max_age_s=900,
    channels=(
        ModelAlarmChannelDef(
            id="slack_alerts",
            kind="slack",
            role="primary",
            token_ref="SLACK_BOT_TOKEN",
            channel_ref="SLACK_CHANNEL_ID",
        ),
        ModelAlarmChannelDef(id="second", kind="in_memory", role="second"),
    ),
)


class FakeSlackApi:
    """A Slack API object whose behaviour is chosen by the test."""

    def __init__(self, *, refuse: str | None = None, shows: bool | None = True) -> None:
        self.refuse = refuse
        self.shows = shows
        self.posted: list[str] = []

    def post(self, text: str) -> str:
        if self.refuse:
            raise AlarmSendRefusedError(self.refuse)
        self.posted.append(text)
        return f"1730000000.{len(self.posted):06d}"

    def exists(self, message_ts: str) -> bool | None:
        return self.shows


def verdict(state: str = "UNKNOWN") -> ModelLabSourceVerdict:
    return ModelLabSourceVerdict.model_validate(
        {
            "id": "runner_groups",
            "title": "CI runner groups",
            "kind": "runner_groups",
            "state": state,
            "probed_at": None,
            "published_at": T0,
            "age_s": None,
            "max_age_s": 900,
            "reason": "planted offline for a drill: the source did not answer",
            "summary": "",
            "subjects": (),
        }
    )


def run_alarm(
    api: FakeSlackApi,
    second: InMemoryAlarmChannel,
    *,
    at: int = 0,
    state: ModelAlarmState | None = None,
    is_test: bool = False,
) -> ModelLabStateAlarmResult:
    handler = HandlerLabStateAlarm(
        {"slack_alerts": SlackAlarmChannel(api), "second": second}
    )
    return handler.handle(
        ModelLabStateAlarmRequest(
            now=T0 + timedelta(seconds=at),
            verdicts=(verdict(),),
            state=state or ModelAlarmState(),
            overlay=OVERLAY,
            is_test=is_test,
        )
    )


# --- the Slack channel -------------------------------------------------------------------------------------------------


def test_golden_an_alarm_is_posted_to_the_alert_channel_and_read_back_as_delivered() -> (
    None
):
    api, second = FakeSlackApi(), InMemoryAlarmChannel()
    result = run_alarm(api, second)
    sends = result.state.open["runner_groups"].sends
    assert [(s.channel, s.delivery) for s in sends] == [("slack_alerts", "delivered")]
    assert sends[0].receipt_id == "1730000000.000001"
    assert sends[0].read_back_at is not None
    assert result.undelivered == ()
    assert second.sent == []
    assert api.posted[0].startswith("Lab alarm: CI runner groups: UNKNOWN.")


def test_golden_a_drill_alarm_says_it_is_a_test_in_the_message() -> None:
    api = FakeSlackApi()
    run_alarm(api, InMemoryAlarmChannel(), is_test=True)
    assert api.posted[0].startswith("TEST, not a real fault (floor alarm drill):")


def test_error_slack_refusing_the_post_sends_the_alarm_to_the_second_channel() -> None:
    api = FakeSlackApi(refuse="Slack refused the post: channel_not_found")
    second = InMemoryAlarmChannel()
    result = run_alarm(api, second)
    sends = result.state.open["runner_groups"].sends
    assert [(s.channel, s.delivery) for s in sends] == [
        ("slack_alerts", "failed"),
        ("second", "pending"),
    ]
    assert "channel_not_found" in sends[0].detail
    assert result.escalated == ("runner_groups",)
    later = run_alarm(api, second, at=10, state=result.state)
    settled = later.state.open["runner_groups"].sends
    assert [(s.channel, s.delivery) for s in settled] == [
        ("slack_alerts", "failed"),
        ("second", "delivered"),
    ]


def test_error_a_message_the_channel_never_shows_goes_to_the_second_channel_after_the_window() -> (
    None
):
    api, second = FakeSlackApi(shows=None), InMemoryAlarmChannel()
    first = run_alarm(api, second)
    assert first.undelivered == ("runner_groups",)
    assert second.sent == []
    later = run_alarm(api, second, at=301, state=first.state)
    assert later.escalated == ("runner_groups",)
    assert len(second.sent) == 1


def test_golden_and_error_the_web_api_checks_the_response_and_never_prints_a_secret() -> (
    None
):
    calls: list[tuple[str, dict[str, str]]] = []

    def http(
        url: str, body: bytes, headers: Mapping[str, str], timeout: int
    ) -> tuple[int, bytes]:
        payload = json.loads(body)
        calls.append((url, payload))
        assert headers["Authorization"] == f"Bearer {SECRET_TOKEN}"
        if url.endswith("chat.postMessage"):
            if payload["text"].startswith("REFUSE"):
                return 200, b'{"ok": false, "error": "invalid_auth"}'
            return 200, b'{"ok": true, "ts": "1730000000.000100"}'
        assert url.endswith("conversations.history")
        if payload["latest"] == "1730000000.000100":
            return 200, b'{"ok": true, "messages": [{"ts": "1730000000.000100"}]}'
        if payload["latest"] == "1730000000.000777":
            return 200, b'{"ok": false, "error": "missing_scope"}'
        return 200, b'{"ok": true, "messages": []}'

    api = SlackWebApi(
        credentials=OperatorEnvCredentials(
            {"SLACK_BOT_TOKEN": SECRET_TOKEN, "SLACK_CHANNEL_ID": SECRET_CHANNEL}, None
        ),
        token_ref="SLACK_BOT_TOKEN",
        channel_ref="SLACK_CHANNEL_ID",
        timeout_s=5,
        http=http,
    )
    assert api.post("hello") == "1730000000.000100"
    assert api.exists("1730000000.000100") is True
    assert api.exists("1730000000.999999") is False
    # A channel that cannot be read is not "not delivered"; it is unknown, and the window decides.
    assert api.exists("1730000000.000777") is None
    with pytest.raises(AlarmSendRefusedError) as refused:
        api.post("REFUSE this")
    assert "invalid_auth" in str(refused.value)
    assert SECRET_TOKEN not in str(refused.value)
    assert SECRET_CHANNEL not in str(refused.value)


def test_error_a_credential_that_cannot_be_resolved_is_a_typed_refusal_naming_only_the_ref() -> (
    None
):
    api = SlackWebApi(
        credentials=OperatorEnvCredentials({}, None),
        token_ref="SLACK_BOT_TOKEN",
        channel_ref="SLACK_CHANNEL_ID",
        timeout_s=5,
        http=lambda *_a: (_ for _ in ()).throw(
            AssertionError("no call without a credential")
        ),
    )
    with pytest.raises(AlarmSendRefusedError) as refused:
        api.post("x")
    assert "SLACK_BOT_TOKEN and SLACK_CHANNEL_ID" in str(refused.value)
    assert api.exists("1.2") is None


def test_error_a_network_failure_or_a_non_json_answer_is_a_refusal_not_a_delivery() -> (
    None
):
    def make(http: Callable[..., tuple[int, bytes]]) -> SlackWebApi:
        return SlackWebApi(
            credentials=OperatorEnvCredentials({"A": "t", "B": "c"}, None),
            token_ref="A",
            channel_ref="B",
            timeout_s=5,
            http=http,
        )

    def down(*_a: object) -> tuple[int, bytes]:
        raise OSError("unreachable")

    for bad in (down, lambda *_a: (200, b"<html>"), lambda *_a: (429, b"{}")):
        with pytest.raises(AlarmSendRefusedError):
            make(bad).post("x")


def test_golden_a_named_key_is_read_from_the_env_file_and_the_file_is_never_sourced(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / "operator.env"
    env_file.write_text(
        "# comment\nOTHER=1\nexport SLACK_BOT_TOKEN='old'\nSLACK_BOT_TOKEN=\"new-value\"\nSLACK_CHANNEL_ID=C1\n",
        encoding="utf-8",
    )
    assert read_named_key(env_file, "SLACK_BOT_TOKEN") == "new-value"
    assert read_named_key(env_file, "ABSENT") is None
    assert read_named_key(tmp_path / "missing.env", "SLACK_BOT_TOKEN") is None
    creds = OperatorEnvCredentials({"OPERATOR_ENV": str(env_file)}, "OPERATOR_ENV")
    assert creds.get("SLACK_CHANNEL_ID") == "C1"
    shadowed = OperatorEnvCredentials(
        {"OPERATOR_ENV": str(env_file), "SLACK_CHANNEL_ID": "from-env"}, "OPERATOR_ENV"
    )
    assert shadowed.get("SLACK_CHANNEL_ID") == "from-env"


def test_alarm_text_has_no_more_than_the_title_and_reason() -> None:
    alarm = ModelAlarm(
        alarm_id="a",
        source_id="s",
        title="Disk: NOT WORKING",
        detail="h051 has 4 GB free",
        raised_at=T0,
    )
    assert alarm_text(alarm) == "Lab alarm: Disk: NOT WORKING. h051 has 4 GB free"


# --- the canary publication --------------------------------------------------------------------------------------------

CANARY = ModelCanaryPublishOverlay(
    remote="git@example.invalid:org/canary.git",
    branch="canary-status",
    file_name="status.json",
    floor_max_age_s=900,
    heartbeat_max_age_s=900,
    committer_name="floor-canary",
    committer_email="floor-canary@example.invalid",
)


class GitScript:
    """A stand-in for git that records its calls and can fail one verb."""

    def __init__(self, *, fail: str | None = None, has_commit: bool = False) -> None:
        self.fail = fail
        self.has_commit = has_commit
        self.calls: list[tuple[str, ...]] = []

    def __call__(
        self, argv: Sequence[str], cwd: Path, timeout_s: int
    ) -> tuple[int, str]:
        self.calls.append(tuple(argv))
        verb = next(
            a
            for a in argv
            if a in {"init", "remote", "add", "commit", "push", "checkout", "rev-parse"}
        )
        if verb == "init":
            (cwd / ".git").mkdir(exist_ok=True)
        if verb == "rev-parse":
            return (0 if self.has_commit else 1), ""
        if verb == self.fail:
            return 1, "fatal: simulated failure"
        return 0, ""


def status(at: datetime = T0) -> ModelCanaryStatus:
    return ModelCanaryStatus(
        floor_generated_at=at - timedelta(minutes=4),
        alarm_heartbeat_at=at - timedelta(minutes=4),
        published_at=at,
        floor_max_age_s=900,
        heartbeat_max_age_s=900,
    )


def test_golden_the_first_publish_makes_one_commit_and_force_pushes_the_branch(
    tmp_path: Path,
) -> None:
    git = GitScript()
    GitCanaryPublisher(CANARY, tmp_path / "clone", git).publish(status())
    written = json.loads((tmp_path / "clone" / "status.json").read_text())
    assert set(written) == {
        "schema_id",
        "floor_generated_at",
        "alarm_heartbeat_at",
        "published_at",
        "floor_max_age_s",
        "heartbeat_max_age_s",
    }
    verbs = [
        next(
            a
            for a in c
            if a in {"init", "remote", "add", "commit", "push", "checkout", "rev-parse"}
        )
        for c in git.calls
    ]
    assert verbs == ["init", "remote", "add", "rev-parse", "checkout", "commit", "push"]
    assert git.calls[-1][-3:] == ("--force", "origin", "HEAD:refs/heads/canary-status")


def test_golden_a_later_publish_replaces_the_one_commit_so_the_branch_does_not_grow(
    tmp_path: Path,
) -> None:
    git = GitScript(has_commit=True)
    (tmp_path / "clone" / ".git").mkdir(parents=True)
    GitCanaryPublisher(CANARY, tmp_path / "clone", git).publish(status())
    assert any("--amend" in c for c in git.calls)
    assert not any("checkout" in c for c in git.calls)


def test_error_a_failed_push_is_a_typed_error_naming_the_step(tmp_path: Path) -> None:
    git = GitScript(fail="push")
    with pytest.raises(CanaryPublishError) as err:
        GitCanaryPublisher(CANARY, tmp_path / "clone", git).publish(status())
    assert "push" in str(err.value) or "-c" in str(err.value) or "git" in str(err.value)
    assert "fatal: simulated failure" in str(err.value)


# --- the cycle publishes, and the canary reads ---------------------------------------------------------------------------


def canary_overlay_roots(tmp_path: Path) -> dict[str, str]:
    import shutil

    import yaml

    from tests.nodes.node_floor_simple_effect.support import OVERLAYS

    roots = tmp_path / "overlays"
    shutil.copytree(OVERLAYS, roots)
    path = roots / "node_lab_state_alarm_effect" / "overlay.yaml"
    doc = yaml.safe_load(path.read_text())
    doc["canary"] = CANARY.model_dump(mode="json")
    doc["channels"][0] = {
        "id": "slack_alerts",
        "kind": "slack",
        "role": "primary",
        "token_ref": "SLACK_BOT_TOKEN",
        "channel_ref": "SLACK_CHANNEL_ID",
    }
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return {"ONEX_SKILL_OVERLAY_ROOTS": str(roots)}


def tick(
    tmp_path: Path,
    *,
    published: list[ModelCanaryStatus],
    served: tuple[datetime | None, datetime | None] | None,
    api: FakeSlackApi,
    drill: str | None = None,
    fail_publish: bool = False,
    sink: InMemoryEmissionSink | None = None,
    **home_args: Any,
) -> tuple[Path, ModelFloorSimpleTickResult]:
    home = build_home(tmp_path, **home_args)
    env = environ(home) | canary_overlay_roots(tmp_path)

    def publish(s: ModelCanaryStatus) -> None:
        if fail_publish:
            raise CanaryPublishError(
                "git push failed (exit 1): fatal: simulated failure"
            )
        published.append(s)

    clock_state = {"n": 0}

    def clock() -> datetime:
        clock_state["n"] += 1
        return NOW + timedelta(seconds=clock_state["n"])

    handler = HandlerFloorSimpleTick(
        sink=sink or InMemoryEmissionSink(),
        clock=clock,
        served_alarm_ids=lambda: None,
        slack_apis={"slack_alerts": api},
        canary_publisher=publish,
        served_times=lambda: served,
    )
    result = handler.run(
        ModelFloorSimpleTickRequest(home=str(home), drill_offline=drill), environ=env
    )
    return home, result


def test_golden_the_cycle_publishes_what_the_served_page_says_for_the_canary(
    tmp_path: Path,
) -> None:
    published: list[ModelCanaryStatus] = []
    served = (NOW - timedelta(minutes=5), NOW - timedelta(minutes=5))
    sink = InMemoryEmissionSink()
    _home, result = tick(
        tmp_path, published=published, served=served, api=FakeSlackApi(), sink=sink
    )
    assert result.ok
    assert result.canary == "published"
    assert len(published) == 1
    assert published[0].floor_generated_at == served[0]
    assert published[0].alarm_heartbeat_at == served[1]
    assert (published[0].floor_max_age_s, published[0].heartbeat_max_age_s) == (
        900,
        900,
    )
    chain = [e for e in sink.events if e.node.endswith("publish_canary")]
    assert [e.phase for e in chain] == ["start", "success"]
    assert chain[0].correlation_id != result.correlation_id


def test_error_a_publish_that_fails_is_a_typed_failure_on_its_own_chain_and_the_cycle_still_writes(
    tmp_path: Path,
) -> None:
    published: list[ModelCanaryStatus] = []
    sink = InMemoryEmissionSink()
    _home, result = tick(
        tmp_path,
        published=published,
        served=(NOW, NOW),
        api=FakeSlackApi(),
        fail_publish=True,
        sink=sink,
    )
    assert result.ok
    assert result.canary == "failed_unexpected_CanaryPublishError"
    chain = [e for e in sink.events if e.node.endswith("publish_canary")]
    assert [(e.phase, e.failure_code) for e in chain] == [
        ("start", None),
        ("failure", "unexpected_CanaryPublishError"),
    ]


def test_error_an_unreadable_served_page_publishes_nothing_so_the_canary_goes_silent(
    tmp_path: Path,
) -> None:
    published: list[ModelCanaryStatus] = []
    _home, result = tick(tmp_path, published=published, served=None, api=FakeSlackApi())
    assert result.canary == "unpublished_served_page_unread"
    assert published == []


def test_golden_a_planted_offline_source_alarms_in_slack_as_a_test_and_publishes_no_canary(
    tmp_path: Path,
) -> None:
    published: list[ModelCanaryStatus] = []
    api = FakeSlackApi()
    home, result = tick(
        tmp_path,
        published=published,
        served=(NOW, NOW),
        api=api,
        drill="runner_groups",
    )
    assert result.ok
    assert result.canary == "skipped_drill"
    assert published == []
    assert len(api.posted) == 1
    assert api.posted[0].startswith("TEST, not a real fault (floor alarm drill):")
    assert "CI runner groups" in api.posted[0]
    doc = json.loads(
        (home / ".claude_scratch/factory-floor/floor-simple.json").read_text()
    )
    assert [a["delivery"] for a in doc["alarms"]] == ["delivered"]
    assert [a["channel"] for a in doc["alarms"]] == ["slack_alerts"]


def test_error_a_drill_naming_a_source_the_lab_does_not_declare_is_a_typed_failure(
    tmp_path: Path,
) -> None:
    _home, result = tick(
        tmp_path, published=[], served=(NOW, NOW), api=FakeSlackApi(), drill="nonesuch"
    )
    assert not result.ok
    assert result.failure_code == "drill_unknown_source"


# --- the hosted canary's verdict ---------------------------------------------------------------------------------------


def write_status(path: Path, s: ModelCanaryStatus) -> Path:
    path.write_text(s.model_dump_json(), encoding="utf-8")
    return path


def test_golden_a_fresh_status_passes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    file = write_status(tmp_path / "s.json", status(T0))
    code = canary(
        ["--canary-status", str(file), "--now", (T0 + timedelta(minutes=5)).isoformat()]
    )
    assert code == 0
    assert json.loads(capsys.readouterr().out)["state"] == "ok"


@pytest.mark.parametrize(
    ("minutes_later", "state"),
    [(20, "publisher_silent")],
)
def test_error_a_status_nobody_has_refreshed_is_silent(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    minutes_later: int,
    state: str,
) -> None:
    file = write_status(tmp_path / "s.json", status(T0))
    code = canary(
        [
            "--canary-status",
            str(file),
            "--now",
            (T0 + timedelta(minutes=minutes_later)).isoformat(),
        ]
    )
    captured = capsys.readouterr()
    assert code == 3
    assert json.loads(captured.out)["state"] == state
    assert "SILENT" in captured.err


def test_error_a_floor_page_that_stopped_updating_is_silent_even_though_publishing_continues(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    stale = ModelCanaryStatus(
        floor_generated_at=T0 - timedelta(minutes=40),
        alarm_heartbeat_at=T0,
        published_at=T0,
        floor_max_age_s=900,
        heartbeat_max_age_s=900,
    )
    file = write_status(tmp_path / "s.json", stale)
    code = canary(["--canary-status", str(file), "--now", T0.isoformat()])
    assert code == 3
    assert json.loads(capsys.readouterr().out)["state"] == "floor_silent"


def test_error_a_missing_or_corrupt_status_is_silence_not_a_pass(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert canary(["--canary-status", str(tmp_path / "absent.json")]) == 2
    (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")
    assert canary(["--canary-status", str(tmp_path / "bad.json")]) == 2
    assert "SILENT" in capsys.readouterr().err
