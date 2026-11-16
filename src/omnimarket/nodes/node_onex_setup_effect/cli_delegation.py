# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""`onex delegation status|on|off`: Claude Code delegation, global (OMN-23413).

The delegation hooks live in Claude Code's user settings and ~/.omninode, so a
`claude` started in any folder delegates once this is on. Nothing here reads
or writes a workspace or the setup journal (operator 2026-11-13: "hooks should
be installed in claude code global, not workspace related"). Claude Code is
the user's own install: a missing or signed-out one is named, never installed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Literal

import click

from omnimarket.handlers.delegation_hook_switch import delegation_switch_enabled
from omnimarket.nodes.node_hook_delegate_effect.handlers.handler_hook_delegate import (
    read_switches,
    write_switches,
)
from omnimarket.nodes.node_hook_delegate_effect.models import ModelDelegationSwitches
from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_claude import (
    SWITCH_OVERRIDE_FIX,
    HandlerSetupClaude,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_observation import (
    ModelSetupObservation,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_plan import (
    ModelSetupPlan,
)
from omnimarket.nodes.node_onex_setup_effect.protocols.protocol_setup_step import (
    SetupStepRefusedError,
)

#: The mask bit the delegation hooks read (OMN-22498).
_SWITCH = "POST_SKILL_DELEGATION_ENFORCER"
#: What the user does when Claude Code is not ready; ONEX never does it for them.
_USER_STEP = {
    "not_installed": "Claude Code is not installed. Install it yourself (claude.com/claude-code), then run onex delegation on again.",
    "signed_out": "Claude Code is not signed in. Run claude and sign in, then run onex delegation on again.",
}
_Step = Literal["claude-plugin", "claude-hooks"]
_STEPS: tuple[_Step, ...] = ("claude-plugin", "claude-hooks")


def _plan() -> ModelSetupPlan:
    # The Claude steps use no workspace; home stands in for the field the plan
    # requires, and nothing is written there. The ONEX plugin profile is the one
    # already registered: a developer install keeps onex@omninode-tools-dev
    # instead of gaining the customer plugin beside it (seen 2026-11-13).
    try:
        developer = HandlerSetupClaude(name="claude-plugin").developer_registered()
    except (OSError, ValueError):
        developer = False  # the plugin step then reports the unreadable registry
    return ModelSetupPlan(
        workspace=Path.home(),
        action="repair",
        components=_STEPS,
        developer=developer,
    )


def _switch(on: bool) -> None:
    onex = str(Path(sys.executable).parent / "onex")
    subprocess.run(
        [onex, "hooks", "enable" if on else "disable", _SWITCH],
        check=True,
        capture_output=True,
        text=True,
    )


def _switch_words() -> dict[str, str]:
    """The prompt hook's switch (~/.onex/delegation.json): on, or paused. It is
    the one switch (OMN-23413 Amendment 14a): subagents always run on Claude."""
    return {"prompts": "on" if read_switches().prompts else "paused"}


def _switch_failed(on: bool) -> ModelSetupObservation:
    verb = "enable" if on else "disable"
    return ModelSetupObservation(
        step="switch",
        status="failure",
        fix=f"onex hooks {verb} {_SWITCH} failed; run it yourself to see why, then run onex delegation {'on' if on else 'off'} again.",
    )


def _claude_code(plugin: ModelSetupObservation) -> str:
    marked = plugin.evidence.get("claude_code")
    if marked in ("not_installed", "signed_out"):
        return str(marked)
    return "ready" if plugin.status == "success" else "installed"


def _advice(observations: list[ModelSetupObservation]) -> str | None:
    """What the user does next. A change they made, or a failure, keeps its own
    message; what `on` adds by itself (a missing plugin or hooks) is one command,
    never `onex setup` (OMN-23413, 2026-11-13: setup is per workspace)."""
    own = [o for o in observations if o.status not in ("success", "unavailable")]
    if own:
        return next((o.fix for o in own if o.fix), None)
    older = [o for o in observations if o.evidence.get("installed") == "older"]
    if older:
        return older[0].fix
    if any(o.status == "unavailable" for o in observations):
        return "Run onex delegation on, or switch it on in onex under /delegation."
    return None


def _report(
    observations: list[ModelSetupObservation],
    status: str,
    json_output: bool,
    *,
    cause: ModelSetupObservation | None = None,
) -> None:
    """``cause`` is the step that stopped the command; its fix leads, so a refused
    off never leads with advice for on (review 2026-11-15)."""
    claude_code = _claude_code(observations[0])
    fix = (cause.fix if cause is not None else None) or (
        _USER_STEP.get(claude_code) or _advice(observations)
    )
    switches = _switch_words()
    if json_output:
        click.echo(
            json.dumps(
                {
                    "status": status,
                    "claude_code": claude_code,
                    "switches": switches,
                    "fix": fix if status != "on" else _advice(observations),
                    "observations": [o.model_dump(mode="json") for o in observations],
                }
            )
        )
        return
    click.echo(f"Claude Code delegation: {status} (Claude Code: {claude_code})")
    if status == "on":
        click.echo(f"Prompts: {switches['prompts']}")
    note = fix if status != "on" else _advice(observations)
    if note:
        click.echo(note)


def _ensure(
    name: _Step,
    plan: ModelSetupPlan,
    observed: ModelSetupObservation | None = None,
    *,
    replace: bool = False,
) -> ModelSetupObservation:
    """Apply one step when it is not ready, then observe it again; an observation
    already taken is used rather than asking Claude Code a second time."""
    step = (
        HandlerSetupClaude(name=name, replace=True)
        if replace
        else HandlerSetupClaude(name=name)
    )
    observed = observed or step.observe(plan)
    if observed.status == "success":
        return observed
    try:
        applied = step.apply(plan)
    except SetupStepRefusedError as refused:
        return ModelSetupObservation(step=name, status="failure", fix=str(refused))
    except Exception:  # its text may carry secrets; say what to do instead
        return ModelSetupObservation(
            step=name,
            status="failure",
            fix=f"The {name} step failed; run onex delegation on again.",
        )
    # Like onex setup, a step that had to be applied reads changed (review 2026-11-15).
    return applied or step.observe(plan).model_copy(update={"changed": True})


@click.group("delegation")
@click.pass_context
def delegation_group(ctx: click.Context) -> None:
    """Claude Code delegation, for every folder you run claude in."""
    ctx.ensure_object(dict)


@delegation_group.command("status")
@click.option("--json", "json_output", is_flag=True)
def status_command(json_output: bool) -> None:
    """Whether Claude Code is there and delegation is on; changes nothing."""
    plan = _plan()
    seen = [HandlerSetupClaude(name=name).observe(plan) for name in _STEPS]
    plugin, hooks = seen
    # An older hook onex installed, untouched, still delegates while the switch
    # is on; it is "on", with the update named (OMN-23413, 2026-11-13).
    older_on = (
        plugin.status == "success"
        and hooks.evidence.get("installed") == "older"
        and not hooks.evidence.get("installed_missing")  # AC38: the prompt hook, or off
        and delegation_switch_enabled()
    )
    on = all(o.status == "success" for o in seen) or older_on
    _report(seen, "on" if on else "off", json_output)


@delegation_group.command("on")
@click.option(
    "--replace",
    is_flag=True,
    help="Copy hooks onex did not write to ~/.omninode/delegation/backups, then reinstall.",
)
@click.option(
    "--prompts/--no-prompts",
    default=True,
    help="Delegate every prompt (the prompt hook). --no-prompts pauses it.",
)
@click.option("--json", "json_output", is_flag=True)
def on_command(replace: bool, prompts: bool, json_output: bool) -> None:
    """Switch Claude Code delegation on, for every folder you run claude in.

    One hook is installed, the prompt hook; --no-prompts installs it paused
    without touching Claude Code's settings again."""
    # One hook (OMN-23413 Amendment 14a, operator 2026-11-17): Claude's own
    # helper tasks always run on Claude, so `on` offers no second switch.
    plan = _plan()
    plugin = HandlerSetupClaude(name="claude-plugin").observe(plan)
    if _claude_code(plugin) in ("not_installed", "signed_out"):  # the user's step
        hooks = HandlerSetupClaude(name="claude-hooks").observe(plan)
        _report([plugin, hooks], "not_ready", json_output)
        raise SystemExit(1)
    if "ONEX_HOOKS_MASK" in os.environ and not delegation_switch_enabled():
        # The shell's mask wins over the switch on would save: installing could
        # never reach "on", so nothing is installed (review 2026-11-15).
        overridden = ModelSetupObservation(
            step="claude-hooks",
            status="action_required",
            evidence={"switch_override": "ONEX_HOOKS_MASK"},
            fix=SWITCH_OVERRIDE_FIX,
        )
        _report([plugin, overridden], "not_ready", json_output)
        raise SystemExit(1)
    plugin = _ensure("claude-plugin", plan, plugin)
    if plugin.status != "success":
        hooks = HandlerSetupClaude(name="claude-hooks").observe(plan)
        _report([plugin, hooks], "not_ready", json_output)
        raise SystemExit(1)
    hooks = _ensure("claude-hooks", plan, replace=replace)
    if hooks.status != "success":
        _report([plugin, hooks], "not_ready", json_output)
        raise SystemExit(1)
    try:
        _switch(True)
    except subprocess.CalledProcessError:
        failed = _switch_failed(True)
        _report([plugin, hooks, failed], "not_ready", json_output, cause=failed)
        raise SystemExit(1) from None
    write_switches(ModelDelegationSwitches(prompts=prompts))
    _report([plugin, hooks], "on", json_output)


@delegation_group.command("off")
@click.option("--json", "json_output", is_flag=True)
def off_command(json_output: bool) -> None:
    """Disable delegation and remove the whole hook, its history helper included.

    Nothing records prompts while delegation is off; the delegation store keeps
    its run history."""
    try:
        _switch(False)
        switch_failed = None
    except subprocess.CalledProcessError:
        # Removing the hooks below still stops delegation; the report says the switch failed.
        switch_failed = _switch_failed(False)
    plan = _plan()
    try:
        # OMN-23413 AC43 (Amendment 14d, operator 2026-11-17): off keeps no
        # history helper, superseding Amendment 13's "OFF retains pending context".
        HandlerSetupClaude(name="claude-hooks").remove_delegation_hooks()
    except Exception:
        refused = ModelSetupObservation(
            step="claude-hooks",
            status="action_required",
            fix=(
                "Delegation is disabled, but its hook removal was refused. "
                "Preserve any edited delegation hooks and review "
                "onex delegation status before retrying off."
            ),
        )
        _report(
            [HandlerSetupClaude(name="claude-plugin").observe(plan), refused],
            "action_required",
            json_output,
            cause=refused,
        )
        raise SystemExit(1) from None
    seen = [HandlerSetupClaude(name=name).observe(plan) for name in _STEPS]
    if switch_failed is not None:
        _report(
            [*seen, switch_failed], "action_required", json_output, cause=switch_failed
        )
        raise SystemExit(1)
    _report(seen, "off", json_output)
