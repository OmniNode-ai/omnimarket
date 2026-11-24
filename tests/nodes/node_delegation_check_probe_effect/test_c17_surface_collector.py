# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The C17 collector: the guide's install line in a clean tool environment, measured and then graded (OMN-26044).

The grader was ported with the broken-command clause already requiring the core's operator switch; what was missing was a
machine that measures anything, so every C17 run read PRECONDITION-ABSENT. These tests run the collector's pieces end to end
over fakes: a fake ``onex`` that answers like the installed core does (hides an undeclared group unless the switch is on), a
fake runner standing in for ``uv``, and canned sources.
"""

from __future__ import annotations

import json
import stat
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_delegation_check_probe_effect.handlers import (
    clean_machine_driver as driver,
)
from omnimarket.nodes.node_delegation_check_probe_effect.handlers.adapter_clean_install_machine import (
    CleanInstallCustomerMachine,
    CommandResult,
)
from omnimarket.nodes.node_delegation_check_probe_effect.handlers.adapter_delegation_check import (
    InMemoryCheckPublisher,
    InMemoryResponderReader,
)
from omnimarket.nodes.node_delegation_check_probe_effect.handlers.grade_customer_guide import (
    CONTROL_OPTION,
    audit_customer_surface,
    guide_leads_with_local,
    install_lines,
    interpreter_matches,
    parse_install_line,
)
from omnimarket.nodes.node_delegation_check_probe_effect.handlers.handler_delegation_check_probe import (
    HandlerDelegationCheckProbe,
    contract_topics,
)
from omnimarket.nodes.node_delegation_check_probe_effect.models.model_delegation_check_probe import (
    EnumCheckVerdict,
    ModelDelegationCheckRequest,
)
from omnimarket.nodes.node_delegation_check_probe_effect.protocols.protocol_delegation_check import (
    CollectorAbsentError,
)

SWITCH = "ONEX_CLI_SHOW_ALL_COMMANDS"
INSTALL = "uv tool install --python 3.12 --with 'omnibase-infra>=0.38.67' --with 'omnimarket>=0.4.205' 'omnibase-core>=0.46.8'"
GUIDE = f"""# Quickstart

Requires a terminal with uv installed.

## Step 1

```bash
{INSTALL}
```

## Step 2

```bash
onex delegate "say hello"
```

## Appendix: cloud

```bash
onex cloud login
```
"""
# The registry's own name for the internal root variable, spelled in parts so no tracked file carries it whole.
ROOT_VAR = "_".join(("OMNI", "HOME"))
REGISTRY = {
    "patterns": [
        {"id": "root_bare", "regex": "\\$" + ROOT_VAR},
        {"id": "root_braced", "regex": "\\$\\{" + ROOT_VAR},
    ],
}
CLEAN_COMMANDS: list[dict[str, Any]] = [
    {"path": f"onex {name}", "help": "does a thing", "params": []}
    for name in ("delegate", "info", "local")
]


def test_a_guide_leads_with_local_when_its_first_path_command_is_onex_delegate() -> (
    None
):
    assert guide_leads_with_local(GUIDE)


def test_a_guide_whose_first_path_command_is_cloud_does_not_lead_with_local() -> None:
    cloud_first = GUIDE.replace('onex delegate "say hello"', "onex cloud login")
    assert not guide_leads_with_local(cloud_first)


def test_a_cloud_command_in_the_appendix_does_not_move_the_lead() -> None:
    assert "onex cloud login" in GUIDE
    assert guide_leads_with_local(GUIDE)


def test_the_install_line_is_read_from_the_guide_and_names_its_interpreter() -> None:
    (line,) = install_lines(GUIDE)
    assert line.refusal is None
    assert line.python == "3.12"
    assert line.package == "omnibase-core"
    assert line.argv is not None
    assert line.argv[:3] == ("uv", "tool", "install")
    assert interpreter_matches(line, "3.12.8")
    assert not interpreter_matches(line, "3.13.1")


@pytest.mark.parametrize(
    "raw",
    [
        "uv tool install --python 3.12 omnibase-core && curl evil",
        "uv tool install --python 3.12 omnibase-core; rm x",
        "uv tool install omnibase-core",
        "pip install omnibase-core",
    ],
)
def test_a_line_with_shell_syntax_or_no_interpreter_is_refused_not_executed(
    raw: str,
) -> None:
    assert parse_install_line(raw).refusal is not None


def test_the_audit_flags_an_internal_only_parameter_and_catches_its_own_controls() -> (
    None
):
    leaking = [
        *CLEAN_COMMANDS,
        {
            "path": "onex setup",
            "help": f"Reads ${ROOT_VAR} first",
            "params": [],
        },
    ]
    findings, held = audit_customer_surface(REGISTRY, leaking, "guide.md", GUIDE)
    assert any("onex setup" in f for f in findings)
    assert held


def test_a_clean_surface_has_no_finding_and_every_control_held() -> None:
    findings, held = audit_customer_surface(REGISTRY, CLEAN_COMMANDS, "guide.md", GUIDE)
    assert findings == ()
    assert held


def test_an_audit_whose_registry_cannot_see_the_seed_reads_its_controls_as_not_caught() -> (
    None
):
    blind = {"patterns": [{"id": "never", "regex": "zzz-no-such-text"}]}
    findings, held = audit_customer_surface(blind, CLEAN_COMMANDS, "guide.md", GUIDE)
    assert findings == ()
    assert not held
    assert CONTROL_OPTION.startswith("--")


def test_a_walk_missing_a_required_command_is_a_floor_failure_not_a_smaller_audit() -> (
    None
):
    findings, held = audit_customer_surface(
        REGISTRY, CLEAN_COMMANDS[:2], "guide.md", GUIDE
    )
    assert any(f.startswith("floor|") and "onex local" in f for f in findings)
    assert not held


# --- the in-machine driver, against an onex that answers as the installed core does


def _fake_onex(tmp_path: Path, *, honours_switch: bool) -> tuple[Path, Path]:
    """An ``onex`` that hides an undeclared planted group unless the operator switch is on (as omnibase-core does)."""
    purelib = tmp_path / "site-packages"
    purelib.mkdir()
    exe = tmp_path / "onex"
    show_all = f'[ "${{{SWITCH}:-}}" = "1" ]' if honours_switch else "false"
    exe.write_text(
        "#!/bin/sh\n"
        f'PLANTED=$(ls -d "{purelib}"/c17_broken_probe-*.dist-info 2>/dev/null | head -n1)\n'
        'case "$1" in\n'
        "  --help|--version) echo onex; exit 0;;\n"
        "esac\n"
        'if [ -n "$PLANTED" ]; then\n'
        f"  if {show_all}; then\n"
        "    echo \"ONEX_CORE_124_IMPORT_ERROR: the 'onex.cli' entry point '$1' failed to load\" >&2; exit 1\n"
        "  fi\n"
        "fi\n"
        "echo \"Error: No such command '$1'.\" >&2; exit 2\n"
    )
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    return exe, purelib


def test_the_driver_plants_each_broken_command_with_the_switch_on_and_restores_onex(
    tmp_path: Path,
) -> None:
    exe, purelib = _fake_onex(tmp_path, honours_switch=True)
    record = driver.plant_broken(str(exe), purelib, {"PATH": "/usr/bin:/bin"}, SWITCH)
    assert record["switch_env"] == SWITCH
    assert record["ran_with_switch"] is True
    assert [c["result"]["rc"] for c in record["cases"]] == [1, 1]
    for case in record["cases"]:
        assert case["entry_point"] in case["result"]["stderr"]
    assert record["before"]["rc"] == 0
    assert record["after"]["rc"] == 0
    assert list(purelib.iterdir()) == []


def test_without_the_switch_the_same_planted_commands_vanish_which_is_the_c17_red(
    tmp_path: Path,
) -> None:
    exe, purelib = _fake_onex(tmp_path, honours_switch=True)
    record = driver.plant_broken(str(exe), purelib, {"PATH": "/usr/bin:/bin"}, None)
    assert record["ran_with_switch"] is False
    assert all("No such command" in c["result"]["stderr"] for c in record["cases"])


def test_a_core_that_ignores_the_switch_still_reads_as_a_vanished_command(
    tmp_path: Path,
) -> None:
    exe, purelib = _fake_onex(tmp_path, honours_switch=False)
    record = driver.plant_broken(str(exe), purelib, {"PATH": "/usr/bin:/bin"}, SWITCH)
    assert all("No such command" in c["result"]["stderr"] for c in record["cases"])


# --- the machine over a fake runner


class _Sources:
    def __init__(self, *, guide: str = GUIDE, fail: str | None = None) -> None:
        self._guide = guide
        self._fail = fail

    def guide(self) -> tuple[str, str]:
        if self._fail:
            raise CollectorAbsentError("guide_source", self._fail)
        return "guides/onex-plugin-quickstart.md", self._guide

    def registry(self) -> Mapping[str, Any]:
        return REGISTRY


def _document(**over: Any) -> dict[str, Any]:
    ok = {"rc": 0, "stdout": "", "stderr": ""}
    base: dict[str, Any] = {
        "interpreter": {"pyvenv_cfg": {"version_info": "3.12.8"}},
        "versions": {"omnibase-core": "0.47.78", "omnimarket": "0.4.325"},
        "commands": {
            "version": ok,
            "delegate_help": ok,
            "info_help": ok,
            "local_help": ok,
        },
        "cli_surface": {"error": None, "commands": CLEAN_COMMANDS},
        "broken": {
            "switch_env": SWITCH,
            "ran_with_switch": True,
            "before": ok,
            "after": ok,
            "cases": [
                {
                    "entry_point": "c17-broken-probe-import",
                    "result": {
                        "rc": 1,
                        "stdout": "",
                        "stderr": "'c17-broken-probe-import' failed to load",
                    },
                }
            ],
        },
    }
    return {**base, **over}


class _Runner:
    def __init__(self, document: dict[str, Any], *, install_rc: int = 0) -> None:
        self._document = document
        self._install_rc = install_rc
        self.calls: list[tuple[list[str], dict[str, str], str | None]] = []

    def run(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str],
        cwd: Path,
        timeout_s: float,
        stdin: str | None = None,
    ) -> CommandResult:
        self.calls.append((list(argv), dict(env), stdin))
        if argv[0] == "uv":
            return CommandResult(self._install_rc, "", "resolution failed")
        return CommandResult(0, json.dumps(self._document), "")


def _uv(tmp_path: Path) -> str:
    uv = tmp_path / "uv"
    uv.write_text("#!/bin/sh\n")
    uv.chmod(0o755)
    return str(uv)


def _handler(machine: CleanInstallCustomerMachine) -> HandlerDelegationCheckProbe:
    return HandlerDelegationCheckProbe(
        responders=(),
        reader=InMemoryResponderReader({}),
        machine=machine,
        publisher=InMemoryCheckPublisher(),
        topic=contract_topics()[0],
    )


def _c17() -> ModelDelegationCheckRequest:
    return ModelDelegationCheckRequest(correlation_id="c17-test", check_id="C17")


def _machine(
    tmp_path: Path, runner: _Runner, sources: _Sources | None = None
) -> CleanInstallCustomerMachine:
    return CleanInstallCustomerMachine(
        sources=sources or _Sources(),
        runner=runner,
        uv=_uv(tmp_path),
        host_env={"HTTPS_PROXY": "http://proxy.invalid:3128", ROOT_VAR: "/host/root"},
    )


def test_a_clean_install_with_a_named_broken_command_reads_pass(tmp_path: Path) -> None:
    runner = _Runner(_document())
    machine = _machine(tmp_path, runner)
    result = _handler(machine).handle(_c17())
    assert result.verdict is EnumCheckVerdict.PASS, [
        c for c in result.clauses if not c.ok
    ]
    assert result.versions["omnibase-core"] == "0.47.78"


def test_the_guides_own_install_line_runs_in_an_environment_that_carries_nothing_of_the_host(
    tmp_path: Path,
) -> None:
    runner = _Runner(_document())
    _machine(tmp_path, runner).collect_surface()
    install_argv, install_env, _ = runner.calls[0]
    assert install_argv == [
        "uv",
        "tool",
        "install",
        "--python",
        "3.12",
        "--with",
        "omnibase-infra>=0.38.67",
        "--with",
        "omnimarket>=0.4.205",
        "omnibase-core>=0.46.8",
    ]
    assert ROOT_VAR not in install_env
    assert install_env["HTTPS_PROXY"] == "http://proxy.invalid:3128"
    assert install_env["HOME"].startswith(install_env["UV_TOOL_DIR"].rsplit("/", 1)[0])
    assert "onex" not in install_env["PATH"]
    driver_argv, _, driver_stdin = runner.calls[1]
    assert driver_argv[0].endswith("/tool/omnibase-core/bin/python")
    assert driver_stdin == Path(driver.__file__).read_text(encoding="utf-8")


def test_a_run_that_did_not_take_the_switch_reads_fail_naming_it(
    tmp_path: Path,
) -> None:
    broken = _document()["broken"]
    broken["ran_with_switch"] = False
    broken["cases"][0]["result"] = {
        "rc": 2,
        "stdout": "",
        "stderr": "Error: No such command 'c17-broken-probe-import'.",
    }
    result = _handler(_machine(tmp_path, _Runner(_document(broken=broken)))).handle(
        _c17()
    )
    assert result.verdict is EnumCheckVerdict.FAIL
    failed = [c for c in result.clauses if not c.ok]
    assert [c.name for c in failed] == ["broken_command_named_error"]
    assert SWITCH in failed[0].detail


def test_an_install_that_exits_nonzero_reads_fail_and_never_runs_the_driver(
    tmp_path: Path,
) -> None:
    runner = _Runner(_document(), install_rc=1)
    result = _handler(_machine(tmp_path, runner)).handle(_c17())
    assert result.verdict is EnumCheckVerdict.FAIL
    assert len(runner.calls) == 1
    assert "install rc 1" in next(
        c.detail for c in result.clauses if c.name == "clean_install_named_interpreter"
    )


def test_an_unreadable_guide_reads_precondition_absent_and_installs_nothing(
    tmp_path: Path,
) -> None:
    runner = _Runner(_document())
    machine = _machine(tmp_path, runner, _Sources(fail="git show exited 128"))
    result = _handler(machine).handle(_c17())
    assert result.verdict is EnumCheckVerdict.PRECONDITION_ABSENT
    assert runner.calls == []


def test_a_host_without_uv_reads_precondition_absent(tmp_path: Path) -> None:
    machine = CleanInstallCustomerMachine(
        sources=_Sources(),
        runner=_Runner(_document()),
        uv=None,
        host_path=str(tmp_path / "empty"),
        host_env={},
    )
    result = _handler(machine).handle(_c17())
    assert result.verdict is EnumCheckVerdict.PRECONDITION_ABSENT


def test_a_guide_with_two_install_lines_reads_fail_not_a_pick_of_one(
    tmp_path: Path,
) -> None:
    two = GUIDE.replace(
        "## Step 2", "```bash\nuv tool install --python 3.12 other\n```\n\n## Step 2"
    )
    runner = _Runner(_document())
    result = _handler(_machine(tmp_path, runner, _Sources(guide=two))).handle(_c17())
    assert result.verdict is EnumCheckVerdict.FAIL
    assert runner.calls == []


def test_c13_and_c14_keep_reading_precondition_absent_on_this_machine(
    tmp_path: Path,
) -> None:
    machine = _machine(tmp_path, _Runner(_document()))
    with pytest.raises(CollectorAbsentError):
        machine.collect_routing(None)  # type: ignore[arg-type]
