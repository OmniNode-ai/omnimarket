# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Refuse a dev runtime container mutation outside a deployment or a grant (OMN-21795).

Operator, 2026-10-31: dev runtime containers may be recreated only by a
deployment or under a grant. The deploy agent recreates the dev runtime from its
own service on the dev host, under the lane lock and with boot-guard recovery. A
Claude session (a lane, a remote lane, or the orchestrator) that runs docker by
hand against that project takes it down outside every one of those protections.
The ledger records it doing so: a restart at 2026-10-28T09:14:41Z, an
unattributed compose up at 09:35:49Z the same day, and a hand compose up at
2026-10-26T00:52:10Z.

What is judged. Every docker or docker-compose invocation in the command, wherever
it sits: after ``ssh host``, ``sudo``, ``timeout`` or ``env``, behind ``xargs``, or
run by its absolute path. The landing worker's PATH shim misses an invocation run
by its absolute path or over ssh from the operator Mac, so this hook reads the
command text instead. An invocation is a mutation of a protected project when:

* it is a compose subcommand from ``compose_mutating_verbs`` (up, down, rm,
  restart, ...), and its project is protected. The project comes from ``-p`` or
  ``--project-name``, else ``COMPOSE_PROJECT_NAME`` set in the command, else a
  ``-f`` file that the policy lists among the project's compose files;
* it is a docker subcommand from ``docker_mutating_verbs`` (rm, restart, stop,
  kill, ...), alone or under ``docker container``, that names one of the
  project's declared container names;
* it is such a docker subcommand, and the command selects containers by the
  project's compose label (``com.docker.compose.project=<project>``), as
  ``docker rm $(docker ps -q --filter label=...)`` does.

Read-only calls (ps, logs, inspect, events, compose config or ps) and every
project the policy does not protect are allowed: the stability lane, the
collaborators' stacks, the per-host dev lanes, the pre-PR slots and the CI bus.

The grant. A mutation passes when the rolling work ledger holds a live grant row
naming the project (see handler_ledger_grant_source). Otherwise the refusal names
the deployment route and the grant row's shape.

Who. Every session in full mode, including the operator's interactive session:
the orchestrator restarted the dev runtime by hand as well. A lite-mode install
(not OmniNode) has no dev runtime and is never refused. A person typing in a shell
is not seen by this hook. The dev host's own alarm covers that case.

What this does not see. A container named by its id, a script file that runs
docker itself, and a command built at run time (``eval``, base64, a variable
holding the project name).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import PurePosixPath

from omnimarket.nodes.node_dev_runtime_mutation_guard_effect.enums import (
    EnumDevRuntimeMutationVerdict,
    EnumGuardSessionMode,
)
from omnimarket.nodes.node_dev_runtime_mutation_guard_effect.handlers.handler_contract_policy_source import (
    ContractGuardPolicySource,
)
from omnimarket.nodes.node_dev_runtime_mutation_guard_effect.handlers.handler_ledger_grant_source import (
    LedgerGrantSource,
)
from omnimarket.nodes.node_dev_runtime_mutation_guard_effect.models import (
    ModelDevRuntimeGuardPolicy,
    ModelDevRuntimeMutationDecision,
    ModelDevRuntimeMutationRequest,
)
from omnimarket.nodes.node_dev_runtime_mutation_guard_effect.protocols import (
    ProtocolGrantSource,
)

#: Splits a command into simple-command segments. Quoting is ignored on purpose: an
#: over-split inside a quoted string (an ssh argument) can only add a segment.
_SEGMENT_SPLIT = re.compile(r"\|\||&&|[;|&()\n`]|\$\(")
_QUOTING = re.compile(r"[\"'\\]")
_PROJECT_ENV = re.compile(r"(?<![\w-])COMPOSE_PROJECT_NAME=[\"']?([A-Za-z0-9_.-]+)")
_LABEL = re.compile(r"com\.docker\.compose\.project=([A-Za-z0-9_.-]+)")

#: docker global options that take a value as the next word.
_DOCKER_VALUE_OPTIONS = frozenset(
    {
        "-H",
        "--host",
        "-c",
        "--context",
        "--config",
        "-l",
        "--log-level",
        "--tlscacert",
        "--tlscert",
        "--tlskey",
    }
)
#: docker compose options that take a value as the next word.
_COMPOSE_VALUE_OPTIONS = frozenset(
    {
        "-p",
        "--project-name",
        "-f",
        "--file",
        "--project-directory",
        "--env-file",
        "--profile",
        "--ansi",
        "--progress",
        "--parallel",
    }
)
_PROJECT_OPTIONS = frozenset({"-p", "--project-name"})
#: Programs whose arguments are text, never a command run: a docker word there is
#: prose (a commit message, an echo, a search pattern), not an invocation.
_TEXT_PROGRAMS = frozenset(
    {
        "echo",
        "printf",
        "git",
        "gh",
        "grep",
        "egrep",
        "fgrep",
        "rg",
        "sed",
        "awk",
        "jq",
        "tee",
        "cat",
        "head",
        "tail",
        "less",
        "man",
        "which",
        "type",
    }
)
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_HEREDOC = re.compile(r"<<-?[ \t]*[\"']?([A-Za-z_][A-Za-z0-9_]*)[\"']?")
_FILE_OPTIONS = frozenset({"-f", "--file"})


def _strip_heredocs(command: str) -> str:
    """The command without its here-document bodies, which are data, never commands."""
    lines = command.split("\n")
    kept: list[str] = []
    waiting: list[str] = []
    for line in lines:
        if waiting:
            if line.strip() == waiting[0]:
                waiting.pop(0)
            continue
        kept.append(line)
        waiting.extend(_HEREDOC.findall(line))
    return "\n".join(kept)


def _is_text_segment(words: list[str]) -> bool:
    """True when the segment's program takes text: its docker words are not invocations."""
    first = next((w for w in words if not _ASSIGNMENT.match(w)), "")
    return _program(first) in _TEXT_PROGRAMS


def _words(segment: str) -> list[str]:
    return _QUOTING.sub(" ", segment).split()


def _program(word: str) -> str:
    return PurePosixPath(word).name


def _compose_call(words: list[str]) -> tuple[str, str | None, list[str]]:
    """The compose subcommand, the -p project (or None) and the -f files of one call."""
    project: str | None = None
    files: list[str] = []
    i = 0
    while i < len(words):
        word = words[i]
        if not word.startswith("-"):
            return word, project, files
        name, eq, value = word.partition("=")
        if name in _COMPOSE_VALUE_OPTIONS:
            if not eq:
                value = words[i + 1] if i + 1 < len(words) else ""
                i += 1
            if name in _PROJECT_OPTIONS:
                project = value
            elif name in _FILE_OPTIONS:
                files.append(value)
        elif word.startswith("-p") and not word.startswith("--") and len(word) > 2:
            project = word[2:]
        i += 1
    return "", project, files


def _docker_call(words: list[str]) -> tuple[str, list[str]]:
    """The docker subcommand after global options, and the words after it."""
    i = 0
    while i < len(words):
        word = words[i]
        if not word.startswith("-"):
            return word, words[i + 1 :]
        if word.partition("=")[0] in _DOCKER_VALUE_OPTIONS and "=" not in word:
            i += 1
        i += 1
    return "", []


class HandlerDevRuntimeMutationGuard:
    """Judges one Bash command against the protected dev runtime projects."""

    def __init__(
        self,
        policy_loader: Callable[[], ModelDevRuntimeGuardPolicy] | None = None,
        grants: ProtocolGrantSource | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._load_policy = policy_loader or ContractGuardPolicySource().load
        self._grants: ProtocolGrantSource = grants or LedgerGrantSource()
        self._clock = clock or (lambda: datetime.now(UTC))

    def mutations(
        self, command: str, policy: ModelDevRuntimeGuardPolicy
    ) -> list[tuple[str, str]]:
        """Every (protected project, invocation phrase) the command would mutate."""
        hits: list[tuple[str, str]] = []
        env_projects = _PROJECT_ENV.findall(command)
        env_project = env_projects[-1] if env_projects else None
        labelled = [p for p in _LABEL.findall(command) if policy.project(p) is not None]
        compose_verbs = set(policy.compose_mutating_verbs)
        docker_verbs = set(policy.docker_mutating_verbs)
        by_file = {
            f: p.project for p in policy.protected_projects for f in p.compose_files
        }
        by_container = {
            c: p.project for p in policy.protected_projects for c in p.containers
        }
        for segment in _SEGMENT_SPLIT.split(_strip_heredocs(command)):
            words = _words(segment)
            if _is_text_segment(words):
                continue
            for index, word in enumerate(words):
                program = _program(word)
                rest = words[index + 1 :]
                if program == "docker-compose":
                    verb, project, files = _compose_call(rest)
                elif program == "docker":
                    sub, after = _docker_call(rest)
                    if sub == "compose":
                        verb, project, files = _compose_call(after)
                    else:
                        if sub == "container" and after:
                            sub, after = after[0], after[1:]
                        if sub not in docker_verbs:
                            continue
                        names = [a.lstrip("/") for a in after if not a.startswith("-")]
                        for name in names:
                            if name in by_container:
                                hits.append(
                                    (by_container[name], f"docker {sub} {name}")
                                )
                        for project_name in labelled:
                            hits.append(
                                (
                                    project_name,
                                    f"docker {sub} by the {project_name} compose label",
                                )
                            )
                        continue
                else:
                    continue
                if verb not in compose_verbs:
                    continue
                target = project or env_project
                if target is None:
                    target = next(
                        (by_file[_program(f)] for f in files if _program(f) in by_file),
                        None,
                    )
                if target is not None and policy.project(target) is not None:
                    hits.append((target, f"docker compose {verb} on project {target}"))
        return hits

    def handle(
        self, request: ModelDevRuntimeMutationRequest
    ) -> ModelDevRuntimeMutationDecision:
        allow = ModelDevRuntimeMutationDecision(
            verdict=EnumDevRuntimeMutationVerdict.ALLOW
        )
        if request.mode is EnumGuardSessionMode.LITE or request.tool_name != "Bash":
            return allow
        if "docker" not in request.command:
            return allow
        policy = self._load_policy()
        hits = self.mutations(request.command, policy)
        if not hits:
            return allow
        granted = self._grants.live_grants(policy.grant, self._clock())
        refused = [
            (project, phrase) for project, phrase in hits if project not in granted
        ]
        if not refused:
            project, phrase = hits[0]
            return ModelDevRuntimeMutationDecision(
                verdict=EnumDevRuntimeMutationVerdict.ALLOW,
                project=project,
                mutation=phrase,
                granted=True,
            )
        project, phrase = refused[0]
        declared = policy.project(project)
        what = (
            f"{project} ({declared.description})"
            if declared and declared.description
            else project
        )
        grant = policy.grant
        reason = (
            f"BLOCKED (OMN-21795): `{phrase}` would recreate, remove, stop or restart containers of the "
            f"compose project {what} outside a deployment. Those containers are changed only by a "
            f"deployment or under a grant. To change them, {policy.deployment_route}. Otherwise get a live "
            f"grant: a {' or '.join(grant.row_kinds)} row on the rolling work ledger carrying "
            f"{grant.target_cell}={grant.target_prefix}{project} and {grant.until_cell}=<ISO-8601 time, "
            f"at most {grant.max_grant_hours:g}h after the row>. No live grant names it in "
            f"{self._grants.describe(grant)}. Read-only docker calls (ps, logs, inspect, events) are not refused."
        )
        return ModelDevRuntimeMutationDecision(
            verdict=EnumDevRuntimeMutationVerdict.REFUSE,
            project=project,
            mutation=phrase,
            reason=reason,
        )


__all__ = ["HandlerDevRuntimeMutationGuard"]
