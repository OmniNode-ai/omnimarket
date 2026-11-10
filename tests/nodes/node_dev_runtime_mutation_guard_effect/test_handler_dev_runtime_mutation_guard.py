# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerDevRuntimeMutationGuard.handle: dev runtime containers change only by a deployment or a grant (OMN-21795).

The planted commands are the shapes the ledger recorded taking the dev runtime down
by hand (a restart of omninode-runtime, a compose up --force-recreate on the dev
project, run over ssh from the operator Mac), plus the bypasses the landing worker
PATH shim cannot see (the docker binary run by its absolute path, a removal by the
compose project label).
"""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_dev_runtime_mutation_guard_effect.enums import (
    EnumDevRuntimeMutationVerdict,
    EnumGuardSessionMode,
)
from omnimarket.nodes.node_dev_runtime_mutation_guard_effect.handlers.handler_contract_policy_source import (
    CONTRACT_PATH,
    ContractGuardPolicySource,
    GuardPolicyError,
)
from omnimarket.nodes.node_dev_runtime_mutation_guard_effect.handlers.handler_dev_runtime_mutation_guard import (
    HandlerDevRuntimeMutationGuard,
)
from omnimarket.nodes.node_dev_runtime_mutation_guard_effect.handlers.handler_ledger_grant_source import (
    LedgerGrantSource,
    grants_in,
)
from omnimarket.nodes.node_dev_runtime_mutation_guard_effect.models import (
    ModelDevRuntimeGrantPolicy,
    ModelDevRuntimeMutationDecision,
    ModelDevRuntimeMutationRequest,
)
from omnimarket.nodes.node_dev_runtime_mutation_guard_effect.protocols import (
    ProtocolGrantSource,
)
from omnimarket.nodes.node_dev_runtime_mutation_guard_effect.runtime_dev_runtime_mutation import (
    build_request,
)
from omnimarket.nodes.node_dev_runtime_mutation_guard_effect.runtime_dev_runtime_mutation import (
    main as runtime_main,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 10, 31, 16, 0, tzinfo=UTC)


class _NoGrants:
    def live_grants(
        self, policy: ModelDevRuntimeGrantPolicy, now: datetime
    ) -> frozenset[str]:
        return frozenset()

    def describe(self, policy: ModelDevRuntimeGrantPolicy) -> str:
        return "the fixture ledger"


def _guard(
    grants: ProtocolGrantSource | None = None, environ: dict[str, str] | None = None
) -> HandlerDevRuntimeMutationGuard:
    return HandlerDevRuntimeMutationGuard(
        policy_loader=lambda: ContractGuardPolicySource().load(environ or {}),
        grants=grants or _NoGrants(),
        clock=lambda: NOW,
    )


def _decide(
    command: str,
    *,
    mode: EnumGuardSessionMode = EnumGuardSessionMode.FULL,
    grants: ProtocolGrantSource | None = None,
    environ: dict[str, str] | None = None,
) -> ModelDevRuntimeMutationDecision:
    request = ModelDevRuntimeMutationRequest(
        tool_name="Bash", command=command, mode=mode
    )
    return _guard(grants, environ).handle(request)


# --- AC1: planted refusals --------------------------------------------------------

PLANTED = [
    # The 2026-10-28T09:14:41Z hand restart, run over ssh from the operator Mac.
    "ssh lab-dev-host 'docker restart omninode-runtime'",
    # The 2026-10-26T00:52:10Z hand restore: the deploy agent's own compose line, typed by a lane.
    "ssh lab-dev-host 'cd /data/omninode/omnibase_infra/docker && docker compose -p omnibase-infra "
    "--profile runtime up -d --force-recreate --no-build --pull never --no-deps'",
    # The same project reached through COMPOSE_PROJECT_NAME and through the compose file.
    "COMPOSE_PROJECT_NAME=omnibase-infra docker compose up -d",
    "export COMPOSE_PROJECT_NAME=omnibase-infra; docker compose restart omninode-runtime-effects",
    "docker compose -f docker/docker-compose.infra.yml -f docker/docker-compose.dev-lane.yml down",
    "docker-compose --project-name=omnibase-infra stop",
    # Bypasses the PATH shim cannot see: the binary by its path, sudo, docker container.
    "/usr/bin/docker rm -f omninode-runtime-effects",
    "sudo docker container stop onex-api",
    "timeout 60 docker -H unix:///var/run/docker.sock kill omnibase-infra-redpanda",
    # Removal by the project label.
    "docker rm -f $(docker ps -aq --filter label=com.docker.compose.project=omnibase-infra)",
    "docker ps -q --filter label=com.docker.compose.project=omnibase-infra | xargs docker restart",
]


@pytest.mark.parametrize("command", PLANTED)
def test_ac1_planted_dev_runtime_mutation_is_refused(command: str) -> None:
    decision = _decide(command)
    assert decision.verdict is EnumDevRuntimeMutationVerdict.REFUSE, command
    assert decision.project == "omnibase-infra"
    assert decision.reason.startswith("BLOCKED (OMN-21795)")
    assert "grant=compose:omnibase-infra" in decision.reason
    assert "deploy agent" in decision.reason
    output = decision.hook_payload()["hookSpecificOutput"]
    assert isinstance(output, dict)
    assert output["permissionDecision"] == "deny"


def test_ac1_live_grant_admits_the_mutation(tmp_path: Path) -> None:
    ledger = tmp_path / "ROLLING_WORK_LEDGER.md"
    ledger.write_text(
        "2026-10-31T15:40:00Z | OPERATOR-CONSENT | lane=orchestrator | ticket=OMN-21795 | "
        "grant=compose:omnibase-infra until=2026-10-31T17:00:00Z | APPROVED SCOPE: restart the dev runtime\n",
        encoding="utf-8",
    )
    grants = LedgerGrantSource({"ONEX_LEDGER_PATH": str(ledger)})
    decision = _decide("docker restart omninode-runtime", grants=grants)
    assert decision.verdict is EnumDevRuntimeMutationVerdict.ALLOW
    assert decision.granted is True


@pytest.mark.parametrize(
    "row",
    [
        # Expired.
        "2026-10-31T10:00:00Z | OPERATOR-CONSENT | grant=compose:omnibase-infra until=2026-10-31T11:00:00Z",
        # A standing grant: until is far past max_grant_hours.
        "2026-10-31T10:00:00Z | OPERATOR-CONSENT | grant=compose:omnibase-infra until=2027-10-31T11:00:00Z",
        # A row kind that grants nothing.
        "2026-10-31T15:40:00Z | STATUS | grant=compose:omnibase-infra until=2026-10-31T17:00:00Z",
        # A grant for another project.
        "2026-10-31T15:40:00Z | OPERATOR-CONSENT | grant=compose:omnibase-infra-fixture until=2026-10-31T17:00:00Z",
        # No until.
        "2026-10-31T15:40:00Z | OPERATOR-CONSENT | grant=compose:omnibase-infra",
    ],
)
def test_ac1_dead_or_foreign_grant_does_not_admit(tmp_path: Path, row: str) -> None:
    ledger = tmp_path / "ROLLING_WORK_LEDGER.md"
    ledger.write_text(row + "\n", encoding="utf-8")
    decision = _decide(
        "docker restart omninode-runtime",
        grants=LedgerGrantSource({"ONEX_LEDGER_PATH": str(ledger)}),
    )
    assert decision.verdict is EnumDevRuntimeMutationVerdict.REFUSE


def test_ac1_unreadable_ledger_grants_nothing(tmp_path: Path) -> None:
    for env in ({}, {"ONEX_LEDGER_PATH": str(tmp_path / "missing.md")}):
        decision = _decide(
            "docker restart omninode-runtime", grants=LedgerGrantSource(env)
        )
        assert decision.verdict is EnumDevRuntimeMutationVerdict.REFUSE
        assert "no ledger" in decision.reason


def test_ac1_grants_in_reads_every_named_project() -> None:
    policy = ContractGuardPolicySource().load({}).grant
    text = (
        "2026-10-31T15:40:00Z | GRANT | grant=compose:omnibase-infra grant=compose:other "
        "until=2026-10-31T16:30:00Z\n"
    )
    assert grants_in(text, policy, NOW) == frozenset({"omnibase-infra", "other"})


# --- AC2: the policy is the contract's, extended by an overlay ----------------------


def test_ac2_overlay_adds_a_protected_container(tmp_path: Path) -> None:
    root = tmp_path / "overlays"
    (root / "node_dev_runtime_mutation_guard_effect").mkdir(parents=True)
    (root / "node_dev_runtime_mutation_guard_effect" / "overlay.yaml").write_text(
        yaml.safe_dump(
            {
                "protected_projects": [
                    {
                        "project": "omnibase-infra",
                        "containers": ["omninode-private-sidecar"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    env = {"ONEX_SKILL_OVERLAY_ROOTS": str(root)}
    assert (
        _decide("docker stop omninode-private-sidecar").verdict
        is EnumDevRuntimeMutationVerdict.ALLOW
    )
    decision = _decide("docker stop omninode-private-sidecar", environ=env)
    assert decision.verdict is EnumDevRuntimeMutationVerdict.REFUSE
    # The contract's own names stay protected with the overlay present.
    assert (
        _decide("docker stop omninode-runtime", environ=env).verdict
        is EnumDevRuntimeMutationVerdict.REFUSE
    )


def test_ac2_overlay_cannot_remove_or_replace_policy(tmp_path: Path) -> None:
    root = tmp_path / "overlays"
    (root / "node_dev_runtime_mutation_guard_effect").mkdir(parents=True)
    (root / "node_dev_runtime_mutation_guard_effect" / "overlay.yaml").write_text(
        yaml.safe_dump({"grant": {"max_grant_hours": 100000}}), encoding="utf-8"
    )
    with pytest.raises(GuardPolicyError):
        ContractGuardPolicySource().load({"ONEX_SKILL_OVERLAY_ROOTS": str(root)})


def test_ac2_policy_values_come_from_the_contract() -> None:
    raw = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))["guard_policy"]
    policy = ContractGuardPolicySource().load({})
    assert [p.project for p in policy.protected_projects] == [
        p["project"] for p in raw["protected_projects"]
    ]
    assert list(policy.compose_mutating_verbs) == raw["compose_mutating_verbs"]
    assert policy.grant.max_grant_hours == raw["grant"]["max_grant_hours"]


# --- AC3: what is never refused ----------------------------------------------------

ALLOWED = [
    "docker ps -a --filter label=com.docker.compose.project=omnibase-infra",
    "ssh lab-dev-host 'docker logs --tail 50 omninode-runtime'",
    "docker inspect omninode-runtime --format '{{.State.Status}}'",
    "docker events --since 30m --filter label=com.docker.compose.project=omnibase-infra",
    "docker compose -p omnibase-infra ps",
    "docker compose -p omnibase-infra config --services",
    "docker compose -p omnibase-infra logs -f omninode-runtime",
    # Other projects, including one whose name extends the protected one, and other container names.
    "docker compose -p omnibase-infra-fixture-lane up -d --force-recreate",
    "docker compose -p fixture-stack -f docker/docker-compose.infra.yml up -d",
    "COMPOSE_PROJECT_NAME=fixture-stack docker compose -f docker/docker-compose.infra.yml down",
    "docker compose -f docker/docker-compose.fixture.yml up -d --no-deps redpanda",
    "docker restart fixture-container",
    "docker restart omninode-runtime-fixture",
    # Prose naming a mutation is not one.
    "git commit -m 'refuse docker restart omninode-runtime by hand'",
    "echo 'docker compose -p omnibase-infra up -d' >> notes.txt",
    "cat > note.md <<'EOF'\ndocker compose -p omnibase-infra down\nEOF",
    "grep -rn 'docker rm omninode-runtime' docs/",
]


@pytest.mark.parametrize("command", ALLOWED)
def test_ac3_allow_read_only_and_other_projects(command: str) -> None:
    assert _decide(command).verdict is EnumDevRuntimeMutationVerdict.ALLOW, command


def test_ac3_allow_lite_mode_and_non_bash() -> None:
    assert _decide(
        "docker restart omninode-runtime", mode=EnumGuardSessionMode.LITE
    ).verdict is (EnumDevRuntimeMutationVerdict.ALLOW)
    request = ModelDevRuntimeMutationRequest(
        tool_name="Edit", command="docker restart omninode-runtime"
    )
    assert _guard().handle(request).verdict is EnumDevRuntimeMutationVerdict.ALLOW


def test_build_request_reads_the_payload() -> None:
    request = build_request(
        {"tool_name": "Bash", "tool_input": {"command": "docker ps"}, "cwd": "/w"},
        "/fallback",
    )
    assert (request.tool_name, request.command, request.cwd) == (
        "Bash",
        "docker ps",
        "/w",
    )


# --- OMN-21989: read-only calls are never refused, a mutation is never smuggled in ----

FIXTURE = Path(__file__).parent / "fixtures" / "recorded_refused_calls.json"

READONLY_ALLOW = [
    "docker events --since 10m --until 1m",
    "docker events --since 30m --filter label=com.docker.compose.project=omnibase-infra",
    "ssh lab-dev-host 'docker events --since 10m --filter container=omninode-runtime'",
    "docker ps",
    "docker ps -a --filter name=omnibase-infra-postgres",
    "ssh lab-dev-host docker ps --filter label=com.docker.compose.project=omnibase-infra",
    "docker inspect omnibase-infra-postgres",
    "docker logs --tail 20 omnibase-infra-postgres",
    "docker compose -p omnibase-infra config",
    "docker compose -f docker/docker-compose.infra.yml config --quiet",
    "ssh lab-dev-host 'docker compose -p omnibase-infra config'",
    # up --dry-run changes nothing, whichever way it names the project.
    "docker compose -p omnibase-infra up --dry-run",
    "docker compose -p omnibase-infra up -d --dry-run",
    "docker compose -p omnibase-infra up --dry-run=true",
    "docker compose -p omnibase-infra --dry-run up -d",
    "docker compose --dry-run -p omnibase-infra up -d --no-build",
    'docker compose -p "omnibase-infra" up --dry-run',
    "docker-compose -p omnibase-infra up --dry-run",
    "COMPOSE_PROJECT_NAME=omnibase-infra docker compose up --dry-run",
    "docker compose -f docker/docker-compose.infra.yml -f docker/docker-compose.dev-lane.yml up -d --dry-run",
    "ssh lab-dev-host 'docker compose -p omnibase-infra up -d --dry-run'",
    "ssh lab-dev-host docker compose -p omnibase-infra up -d --dry-run",
    "ssh lab-dev-host 'cd /data/omninode/omnibase_infra/docker && docker compose -p omnibase-infra --profile runtime up -d --no-build --dry-run'",
    "sudo docker compose -p omnibase-infra up --dry-run omninode-runtime",
    # A later --dry-run after an earlier =false: the last one is the one compose honours.
    "docker compose -p omnibase-infra up --dry-run=false --dry-run",
]


def _recorded_calls() -> list[tuple[str, str]]:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert data["expect"] == "allow"
    return [(call["id"], call["command"]) for call in data["calls"]]


RECORDED = _recorded_calls()


@pytest.mark.parametrize("command", READONLY_ALLOW)
def test_readonly_allow_is_never_refused(command: str) -> None:
    decision = _decide(command)
    assert decision.verdict is EnumDevRuntimeMutationVerdict.ALLOW, command
    assert decision.project == ""
    assert decision.granted is False


@pytest.mark.parametrize(("call_id", "command"), RECORDED, ids=[i for i, _ in RECORDED])
def test_readonly_allow_recorded_refused_calls(call_id: str, command: str) -> None:
    assert _decide(command).verdict is EnumDevRuntimeMutationVerdict.ALLOW, command


@pytest.mark.parametrize(("call_id", "command"), RECORDED, ids=[i for i, _ in RECORDED])
def test_readonly_allow_recorded_refused_calls_entrypoint_exits_zero(
    call_id: str,
    command: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("ONEX_LEDGER_PATH", raising=False)
    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS", raising=False)
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    assert runtime_main([]) == 0
    assert capsys.readouterr().out == ""


COMPOSE_MUTATING = [
    "up -d",
    "up",
    "down",
    "rm -f",
    "restart",
    "stop",
    "kill",
    "start",
    "create",
    "pause",
    "unpause",
    "scale omninode-runtime=2",
]


@pytest.mark.parametrize("verb", COMPOSE_MUTATING)
@pytest.mark.parametrize(
    "shape",
    [
        "docker compose -p omnibase-infra {verb}",
        "ssh lab-dev-host 'docker compose -p omnibase-infra {verb}'",
        "COMPOSE_PROJECT_NAME=omnibase-infra docker compose {verb}",
        "docker compose -f docker/docker-compose.infra.yml {verb}",
    ],
)
def test_refuse_every_compose_mutating_verb_without_a_grant(
    shape: str, verb: str
) -> None:
    command = shape.format(verb=verb)
    decision = _decide(command)
    assert decision.verdict is EnumDevRuntimeMutationVerdict.REFUSE, command
    assert decision.project == "omnibase-infra"


@pytest.mark.parametrize("verb", COMPOSE_MUTATING)
def test_refuse_every_compose_mutating_verb_unless_granted(
    verb: str, tmp_path: Path
) -> None:
    ledger = tmp_path / "ROLLING_WORK_LEDGER.md"
    ledger.write_text(
        "2026-10-31T15:40:00Z | OPERATOR-CONSENT | ticket=OMN-21795 | "
        "grant=compose:omnibase-infra until=2026-10-31T17:00:00Z | fixture\n",
        encoding="utf-8",
    )
    command = f"docker compose -p omnibase-infra {verb}"
    granted = _decide(
        command, grants=LedgerGrantSource({"ONEX_LEDGER_PATH": str(ledger)})
    )
    assert granted.verdict is EnumDevRuntimeMutationVerdict.ALLOW
    assert granted.granted is True
    assert _decide(command).verdict is EnumDevRuntimeMutationVerdict.REFUSE


@pytest.mark.parametrize(
    "command",
    [
        "docker rm -f omnibase-infra-postgres",
        "docker restart omninode-runtime",
        "docker stop omnibase-infra-redpanda",
        "docker kill omnibase-infra-valkey",
        "docker start omnibase-infra-memgraph",
        "docker pause omninode-runtime",
        "docker unpause omninode-runtime",
        "docker rename omninode-runtime omninode-runtime-old",
        "docker update --restart=no omninode-runtime",
        "ssh lab-dev-host 'docker container rm -f omnibase-infra-postgres'",
    ],
)
def test_refuse_every_docker_mutating_verb_without_a_grant(command: str) -> None:
    decision = _decide(command)
    assert decision.verdict is EnumDevRuntimeMutationVerdict.REFUSE, command
    assert decision.project == "omnibase-infra"


#: Commands that carry the text --dry-run but are not a compose dry run of the call.
SMUGGLED_DRY_RUN = [
    # The flag is turned off.
    "docker compose -p omnibase-infra up --dry-run=false",
    "docker compose -p omnibase-infra up -d --dry-run=false",
    "docker compose -p omnibase-infra up --dry-run=0",
    "docker compose -p omnibase-infra up --dry-run=no",
    "docker compose -p omnibase-infra up --dry-run=",
    "docker compose -p omnibase-infra up --dry-run --dry-run=false",
    "ssh lab-dev-host 'docker compose -p omnibase-infra up -d --dry-run=false'",
    # The flag belongs to another command.
    "docker compose -p omnibase-infra up -d && echo --dry-run",
    "docker compose -p omnibase-infra up -d && docker compose -p fixture-stack config --dry-run",
    "docker compose -p omnibase-infra up -d ; docker version --dry-run",
    "docker compose -p omnibase-infra up -d | grep --dry-run",
    "docker compose -p omnibase-infra up -d || true --dry-run",
    "echo --dry-run && docker compose -p omnibase-infra up -d",
    "echo --dry-run; docker compose -p omnibase-infra up -d",
    "docker compose -p omnibase-infra up -d $(echo --dry-run)",
    "docker compose -p omnibase-infra up -d `echo --dry-run`",
    "docker compose -p omnibase-infra up -d\necho --dry-run",
    "ssh lab-dev-host 'docker compose -p omnibase-infra up -d' --dry-run",
    "bash -c 'docker compose -p omnibase-infra up -d' --dry-run",
    # Not an option of the compose call.
    "docker compose -p omnibase-infra up -d -- --dry-run",
    "docker compose -p omnibase-infra --env-file --dry-run up -d",
    "docker compose -p omnibase-infra up --scale --dry-run=1 -d",
    "docker --dry-run compose -p omnibase-infra up -d",
    "FLAG=--dry-run docker compose -p omnibase-infra up -d",
    # A different verb: only up is declared dry-runnable.
    "docker compose -p omnibase-infra down --dry-run",
    "docker compose -p omnibase-infra restart --dry-run",
    "docker compose -p omnibase-infra rm -f --dry-run",
    "docker compose -p omnibase-infra stop --dry-run",
    "docker compose -p omnibase-infra kill --dry-run",
    "docker restart --dry-run omninode-runtime",
    "docker rm --dry-run omnibase-infra-postgres",
]


@pytest.mark.parametrize("command", SMUGGLED_DRY_RUN)
def test_refuse_a_dry_run_flag_that_does_not_make_the_call_a_dry_run(
    command: str,
) -> None:
    decision = _decide(command)
    assert decision.verdict is EnumDevRuntimeMutationVerdict.REFUSE, command
    assert decision.project == "omnibase-infra"


@pytest.mark.parametrize("command", SMUGGLED_DRY_RUN)
def test_refuse_smuggled_dry_run_entrypoint_exits_two(
    command: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("ONEX_LEDGER_PATH", raising=False)
    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS", raising=False)
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    assert runtime_main([]) == 2
    assert "BLOCKED (OMN-21795)" in capsys.readouterr().out


def test_refuse_a_real_up_after_a_dry_run_up_on_the_same_line() -> None:
    command = (
        "docker compose -p omnibase-infra up --dry-run "
        "&& docker compose -p omnibase-infra up -d"
    )
    assert _decide(command).verdict is EnumDevRuntimeMutationVerdict.REFUSE


def test_readonly_allow_dry_run_verbs_come_from_the_contract() -> None:
    raw = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))["guard_policy"]
    policy = ContractGuardPolicySource().load({})
    assert policy.compose_dry_run.flag == raw["compose_dry_run"]["flag"]
    assert list(policy.compose_dry_run.verbs) == raw["compose_dry_run"]["verbs"]
    assert set(policy.compose_dry_run.verbs) <= set(policy.compose_mutating_verbs)


def test_refuse_an_overlay_cannot_widen_the_dry_run_exemption(tmp_path: Path) -> None:
    root = tmp_path / "overlays"
    (root / "node_dev_runtime_mutation_guard_effect").mkdir(parents=True)
    (root / "node_dev_runtime_mutation_guard_effect" / "overlay.yaml").write_text(
        yaml.safe_dump({"compose_dry_run": {"verbs": ["up", "down"]}}),
        encoding="utf-8",
    )
    with pytest.raises(GuardPolicyError):
        ContractGuardPolicySource().load({"ONEX_SKILL_OVERLAY_ROOTS": str(root)})
