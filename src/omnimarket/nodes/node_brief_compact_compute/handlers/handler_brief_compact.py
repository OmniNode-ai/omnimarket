# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""lane-brief's compact brief form and the remote-lane runner's cloud prompt cap (OMN-17427).

FRICTION 2026-10-21T22:17:51Z lane=stability-cloud-2125: a brief with the pasted rules block, 3a and the ticket text
is about 76,000 characters, over CLOUD_PROMPT_MAX, and the runner cut its tail. About 46,000 of them are fixed
lab-lane text a cloud session is told to skip. The compact form replaces each of four such renderings by a checked
reference: a marker pair recording the rendering's sha256, length and parameters around a short stub. Every rendering
is a pure function of the plugin plus those parameters, so expand_brief puts back the exact text, and lane-brief's
check expands before it checks: a compact brief is refused for exactly the reasons its full form would be.

The renderings come from the caller's ``render(name, cells)`` (lane-brief's ``compact_render``); the two lab-lane
bodies the references stand for, 3a.9's delegation routes and 3a.10's heavy-work placement, live here so lane-brief's
``delegation_block`` and ``lab_block`` and the references render one text. ``cloud_prompt`` is the runner's preamble
plus the whole brief; over the cap it raises CloudPromptTooLargeError and never cuts the brief.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping, Sequence

from omnimarket.handlers.node_overlay_reader import resolve_setting
from omnimarket.nodes.node_brief_compact_compute.models.model_brief_compact import (
    ModelBriefCompactRequest,
    ModelBriefCompactResult,
)

CLOUD_PROMPT_MAX = 60000  # the documented /fire text cap is 65,536 characters; the CLI argument stays under it
CLOUD_HEAD_RESERVE = 3000  # what a brief leaves for the runner's preamble
CLOUD_TOO_LARGE_LEAD = "the cloud prompt is "

COMPACT_PARAMS = {
    "rules": (),
    "landing-fixed": (),
    "delegation": ("ticket", "lane", "band"),
    "heavy-work": ("ticket",),
}
COMPACT_LINE_RE = re.compile(r"^<!-- compact-ref\b[^\n]*(?:\n|\Z)", re.MULTILINE)
COMPACT_MARK_RE = re.compile(r"<!-- compact-ref (begin|end) (\S+)(?: (.*?))? -->")
COMPACT_STUBS = {
    "rules": (
        "The rest of the standing rules block ({chars} characters) is lab-lane text: the ledger, inbox, delegation, "
        "lab and landing steps. A cloud session skips it; nothing else in the brief was cut."
    ),
    "landing-fixed": (
        "3a.3's fixed lab-lane steps ({chars} characters: PR creation through onex scm, PR reads and waits through "
        "pr_state_local, git reads, worktree adds and Bash timeouts) stand here. A cloud session skips them."
    ),
    "delegation": (
        "3a.9's delegation routes ({chars} characters: GLM, onex delegate, Jev, Codex and the lab code step, with "
        "their TERMINAL cells) are lab-lane text. A cloud session has none of these routes and skips them."
    ),
    "heavy-work": (
        "3a.10's heavy-work placement ({chars} characters: /omni:lab-run over the lab bus) is lab-lane text. A cloud "
        "session skips it."
    ),
}

Render = Callable[[str, Mapping[str, str]], str]


class CompactRefusedError(ValueError):
    """A brief the compact form cannot carry: a rendering not in it verbatim, or a stale or malformed reference."""


class CloudPromptTooLargeError(ValueError):
    """The cloud prompt is over CLOUD_PROMPT_MAX: the runner refuses it and never cuts the brief (OMN-17427)."""


# The names the plugin callers catch today.
CompactRefused = CompactRefusedError
CloudPromptTooLarge = CloudPromptTooLargeError


def delegation_body(
    ticket: str,
    caller_lane: str,
    band: str | None,
    *,
    output_contract: str,
    glm_first_marker: str,
    codex_command: str,
    knowledge_base: str | None = None,
    registry_root_var: str = "ONEX_REGISTRY_ROOT",
    environ: Mapping[str, str] | None = None,
) -> str:
    """3a.9's text after its heading: the delegation routes and their TERMINAL cells."""
    if knowledge_base is None:
        knowledge_base = resolve_setting(
            "node_brief_compact_compute",
            "knowledge_base",
            fields=frozenset({"knowledge_base"}),
            env_vars=("ONEX_KNOWLEDGE_BASE_REPO",),
            environ=environ,
        )
    band_cell = f" Include `band={band}` on the TERMINAL." if band else ""
    return "\n".join(
        [
            (
                "Text-shaped steps in this lane go through the receipted text step first (GLM through "
                "Claude Code, then `onex delegate` on the lab): PR bodies and PR "
                "comments, commit messages, verify and lab-readback summaries, report and "
                "TERMINAL prose, ticket comments, and classification of logs and CI failures "
                "(`--task-type reasoning`). Code, exact counts, lists of identifiers, "
                "verbatim quotes and error strings stay deterministic: compute them in the shell "
                "and pass them to the prompt as a fact table."
            ),
            "",
            (
                "Agent-posted Linear comments have `actor: <lane> (<model>)` as the first line. "
                "Lane and model come from the actual execution identity, never the Linear account "
                "author. Prefix the comment body file or posting tool body before sending it."
            ),
            "",
            (
                f'    "${registry_root_var}/omnibase_infra/scripts/onex" delegate '
                '"<Summarize|Rewrite|Classify ...: one instruction line, then the fact table>" '
                f"--task-type document --ticket {ticket} --caller-lane {caller_lane} --bus kafka "
                f'--lane dev --locus deployed-lane --state-root "${registry_root_var}/.onex_state"'
            ),
            "",
            (
                "If the deployed lane refuses with no bound consumer (a runtime merge in the last "
                "15 minutes), retry once with `--bus inmemory --locus in-process` in place of the "
                "three lane flags. Pass `--task-type` always; never pass `--criteria-mode "
                "replace-task-class`."
            ),
            "",
            (
                "Every text prompt you write for a direct onex delegate or harness_delegate.py call "
                "ends with this paragraph, verbatim, after a blank line (delegation_step.py text and "
                "landing_text.py append it themselves, so a prompt file for them needs no copy):"
            ),
            "",
            "> " + output_contract,
            "",
            (
                "GLM (z.ai) is a delegation target beside Jev and the lab models (operator "
                "rulings 2026-09-26, OMN-19432, and 2026-09-28T06:24:10Z, OMN-17427), used only "
                "through Claude Code: the Coding Plan terms allow its quota only inside a supported "
                "tool, so the direct GLM rung of `onex delegate` (the cloud-glm backend pin) is "
                f"retired and always fails (omnimarket#3136, OMN-20173). {glm_first_marker}: the "
                "Coding Plan sat at 1% used while GLM was the last rung, so the receipted step tries "
                "glm-5.3-flash, then glm-5.3, through `/omni:harness-delegate --harness claude-glm` "
                "and then the lab through the deployed-lane command above (`--first lab` keeps the "
                "lab-first order; `landing_text.py` drafts the same way):"
            ),
            "",
            (
                '    python3 "${CLAUDE_PLUGIN_ROOT}/skills/merge-drain/scripts/delegation_step.py" '
                f"text --lane <lane> --step <slug> --prompt-file <scratch>/prompt.md --ticket {ticket}"
            ),
            "",
            (
                "For GLM alone (the brief names GLM), run the harness directly; glm-5.3-flash "
                "first, glm-5.3 when flash is not good enough:"
            ),
            "",
            (
                '    python3 "${CLAUDE_PLUGIN_ROOT}/skills/harness-delegate/scripts/harness_delegate.py" '
                "run --harness claude-glm --model glm-5.3-flash --task-class document "
                "--prompt-file <scratch>/prompt.md --check 'test -s \"$HARNESS_ANSWER_FILE\"' "
                f'--lane <lane> --ticket {ticket} --out-dir "${registry_root_var}/.onex_state/harness-delegate"'
            ),
            "",
            (
                "(verified 2026-10-01: run 47b89e4f-ead9-4e01-af6e-1ab530975110, claude-glm, "
                "glm-5.3, a 15 KB summary in 38 s, exit 0). The answer is `answer.md` beside "
                "`receipt.json` in the run directory. GLM through the harness serves the "
                "operator's own work only: never run it for a customer tenant."
            ),
            "",
            (
                "Typed decisions go to Jev (TypeSafe System One): a question with a fixed answer "
                "set, such as which class a CI red belongs to, what to do next with a PR, or which "
                "engine a subtask belongs to. How you ask decides how often it is right (the "
                "2026-09-30 Jev usage eval, 149 public items: 66% asked by option name over raw "
                "state, 77% with every option described, 92% with the deciding facts worked out in "
                "code first). Run the receipted step, from your worktree, over any repository but "
                f"{knowledge_base} (operator RULING 2026-10-13T18:34:53Z, OMN-20106):"
            ),
            "",
            (
                '    python3 "${CLAUDE_PLUGIN_ROOT}/skills/merge-drain/scripts/delegation_step.py" '
                "decide --lane <lane> --step <slug> --repo <repo> --preset "
                "<ci-cause|pr-triage|task-class> --own-answer <your answer> --facts-file "
                "<scratch>/raw.json"
            ),
            "",
            (
                "A preset carries the eval's wording and works out the deciding facts in code from "
                "the raw JSON you give it (a check name, the log tail, the other copies of "
                "the check on the head, the check on the base branch; or the PR's checks). For any "
                "other decision, replace `--preset` with `--question` (naming the fields to judge "
                'from), one `--option` per answer, and `--criterion \'{"<option>": "what it '
                "covers, and what it does not\"}'`: every option needs a description or the step "
                "refuses, and a none_of_these option is added for you (Jev answering it is an "
                "abstain and the lab decides). Work the deciding facts out yourself first and pass "
                "only those in `--facts-file`: Jev is good at a bounded choice over supplied facts "
                "and poor at finding them. Facts only, never code; the step refuses "
                f"{knowledge_base} (and its hyphenated alias {knowledge_base.replace('_', '-')}) before "
                "calling out, so for that repository decide yourself and say so. Your own answer "
                f'is recorded in the receipt and never sent to Jev. The raw node is `cd "${registry_root_var}/omnimarket" && env -u '
                "PYTHONPATH uv run --frozen python -m omnimarket.nodes.node_typed_decision_effect "
                "< <scratch>/decision.json` (a JSON request with `correlation_id`, "
                "`work_unit_repository`, `state`, `kind`, `instructions`, and for choice `criteria` "
                "as option -> {what, not_for}); use it only when the step does not fit. The key "
                "resolves from the local credential store by secret_ref; never read or print it. "
                "Act on `answer`, and record the `correlation_id`."
            ),
            "",
            (
                "Self-contained build and fix items use Codex first (operator ruling 2026-09-30, "
                "OMN-20106; the plan period ends about 2026-10-30) on a host that allows it, "
                "then GLM through Claude Code in the worktree (OMN-20844), then the code-edit node on "
                "the lab, then the lab agent (crush, interim); "
                "the lane verifies. This covers mechanical code edits, test scaffolds, lint and type "
                "fixes, conflict-resolution drafts and red-PR fixes. Write the task, exact files, "
                "behaviour and verify commands in your scratchpad, then run:"
            ),
            "",
            "    " + codex_command,
            "",
            (
                "The thread id is the `thread_id` of the first JSON line of codex.jsonl. Never pass "
                "a model flag or model config; the account uses its config default model. Never edit "
                "`~/.codex/config.toml`. MCP start-up errors on stderr do not stop the run. Codex "
                "runs in the lane's own worktree on the host with the binary. Use "
                "`delegation_step.py code --lane <lane> --worktree <worktree> --prompt-file "
                "<scratch>/codex-prompt.txt --scratch <scratch> --writable '<glob>' "
                "--check 'NAME=<argv command>'` to receipt the attempt and fall "
                "through automatically when Codex is missing, exits nonzero or returns no thread id. "
                "The second route is GLM through Claude Code (below); a GLM run that fails or "
                "changes nothing falls through. The third route runs `onex code-edit run` on the lab: each model turn is an "
                "`onex delegate` run and its ids ride in `runs=<run_id>`. Checks are argv, split "
                "with shlex and run in the worktree without a shell, never shell strings. "
                "Repeat `--writable`, `--check` and `--context` as needed; `--max-turns` defaults "
                "to 20 (1..40). A loop counts as one delegated subtask. When `--writable` or "
                "`--check` is absent the node route is skipped; a failed node route also falls "
                "through to crush on the lab model. `lab_crush.py` is the interim fallback, "
                "OMN-20292 pending, and is never required:"
            ),
            "",
            (
                '    python3 "${CLAUDE_PLUGIN_ROOT}/skills/lane-brief/scripts/lab_crush.py" '
                "--worktree <worktree> --scratch <scratch> --task-file <scratch>/task.txt"
            ),
            "",
            (
                "It writes its own crush config under your scratchpad (the operator's crush "
                "config is never read or edited), refuses a directory that is not a linked "
                "worktree, refuses a lab model server that does not list Qwen3.8-27B or holds "
                "under 65536 tokens of context, runs crush with an output cap of 32768 (the "
                "model reasons in its visible output: a cap of 8192 lost one run in two and "
                "16384 stopped a 3,458-character task mid-reasoning, OMN-20084 and OMN-20106), "
                "and prints one "
                "`LAB-CRUSH session=<id> exit=<n> changed=<yes|no>` line. It defaults to the lab's default model server; pass `--base-url` or set `LAB_MODEL_URL` to run it against another lab host with headroom, and do not point it at a model server whose slot holds under 65536 tokens. A run takes 1.5 to 2 times GLM's wall "
                "time, about five minutes for a 130-line script. Read `git diff` and rerun the "
                "tests yourself before you commit, and finish what it left. For the draft node path "
                "you "
                "apply yourself (a function or a patch, with the source pasted into the prompt "
                "as the fact table), the deployed-lane delegate command above takes the code "
                "tier: `--task-type code_generation` (or `refactor`, `test`) in place of "
                "`--task-type document`."
            ),
            "",
            (
                "GLM through Claude Code editing your worktree, flash then glm-5.3, is the route after "
                "Codex (`delegation_step.py code` runs all four routes and receipts each attempt; "
                "`--glm last` keeps it after the lab agent):"
            ),
            "",
            (
                '    python3 "${CLAUDE_PLUGIN_ROOT}/skills/harness-delegate/scripts/harness_delegate.py" '
                "run --harness claude-glm --task-class code_generation --writable-repo <worktree> "
                "--prompt-file <scratch>/task.txt --check '<focused test>' --lane <lane> "
                f'--ticket {ticket} --out-dir "${registry_root_var}/.onex_state/harness-delegate"'
            ),
            "",
            (
                "The lane applies the draft, reads every diff, reruns focused tests and owns the PR."
            ),
            "",
            (
                "Dispose of every delegated answer before your TERMINAL (OMN-20011): say whether "
                "you used, edited, discarded or ignored what each engine gave you, with a reason "
                "code, on the receipt it left:"
            ),
            "",
            (
                '    python3 "${CLAUDE_PLUGIN_ROOT}/skills/merge-drain/scripts/delegation_step.py" '
                f'--omni-home "${registry_root_var}" dispose --receipt <receipt path> --disposition '
                "used|edited|discarded|ignored --reason-code <code>"
            ),
            "",
            (
                "For an `onex delegate` call you ran yourself, pass `--lane <lane> --run <run id>` "
                "in place of `--receipt`. Codes: used = correct_as_is, verified_against_source; "
                "edited = minor_fix, partial_use, reformatted; discarded = wrong_answer, "
                "hallucinated, off_task, incomplete, unusable_format; ignored = not_needed, "
                "superseded, engine_failed, lane_decided_first. The ledger refuses a TERMINAL that "
                "names a receipt with no disposition (OMN-20015)."
            ),
            "",
            (
                "Claude (you) plans, reviews and verifies: you read every delegated diff and "
                "draft, run the tests, and make every call that touches a gate, the ledger, a "
                "merge or an operator's words. Spend your own turns on a subtask only when its "
                "engine refused it or its output failed your check, and say which in the "
                "TERMINAL."
            ),
            "",
            (
                "Fact-check before use: every number, id, sha, path and name in the draft must "
                "appear in your fact table; recount anything counted; a draft that states a "
                "decision, an approval or an operator's words that the facts do not contain is "
                "discarded. A quality score of 1.0 is not evidence. Record the grade."
            ),
            "",
            (
                "TERMINAL cells, one of: `delegated=<n> runs=<run_id>[,<run_id>...] "
                "codex=<thread_id>[,...] jev=<correlation_id>[,...] lab_agent=<session id>[,...]` (n counts every "
                "delegated subtask, a lab crush draft included; a code-edit loop counts as one and its turn run ids ride in "
                "`runs=`. Runs are the directory names "
                f"under `${registry_root_var}/.onex_state/runs/`. A lab session also rides in `runs=` for the "
                "ledger grammar; Codex carries its thread in `codex=`. Paste the receipt-backed "
                "`delegation_step.py cells` output and leave out an engine cell you did not use), or "
                "`delegated=0 delegation_reason=<reason>` with <reason> one of no-text-or-code, "
                "route-refused:<status>, route-unavailable:<run id>, read-only-lane (operator "
                "ruling 2026-10-01: delegation is a required step of every lane; the ledger refuses "
                "any other reason, and a zero with a declared reason is never blocked). A TERMINAL "
                "with neither is refused, listed by `/omni:delegation-health` and counted as silent "
                "by the morning delegation report." + band_cell
            ),
        ]
    )


# 3a.10's text after its heading and the contract step: where heavy work runs.
HEAVY_WORK = "\n".join(
    [
        (
            "Heavy work is placed on the pool host with the most free capacity and sent "
            "over the event bus (Operating Rule 21, OMN-20105): a test run beyond one "
            "focused file, a build, lint over a tree, and a headless agent run (the lab code step "
            "of 3a.9 included) go through `/omni:lab-run`. Codex runs in the lane's own "
            "worktree on the host with its binary; `--need codex` is not valid because no lab "
            "host has Codex. The pool is "
            "every lab host plus the launching host, which takes work only under the same "
            "load bar. Push the branch first; the unit runs at the pushed sha and never "
            "ships a working tree:"
        ),
        "",
        (
            '    python3 "${CLAUDE_PLUGIN_ROOT}/scripts/onex_lab_run.py" --via bus --lane <lane> '
            "--repo <repo> --ref <pushed branch> -- <command...>"
        ),
        "",
        (
            "Placement reads each host's capacity advertisement on the bus (cores minus "
            "load, free memory, tools), the chosen host's `onex lab-work serve` runs the "
            "command, and the receipt (exit code, log path on that host, output tail) comes "
            "back as an event; the runner exits with the command's exit code. Add `--need claude-login` for a headless Claude "
            "run. Exit 75 means every host is over the load bar: report the placement it "
            "printed and wait or stop, and never run the command on the launching host "
            "instead. Exit 69 means no host advertised: rerun with `--via ssh` (the interim "
            "probe) and say so in the report. Exit 66 means the branch is not pushed. A "
            "focused test file of the change may still run in your own worktree; the "
            "heavy-work guard refuses anything heavier here while this host has no headroom. "
            "Put the host, the sha and the exit code in the PR body."
        ),
    ]
)


def compact_cells(claim: str) -> dict[str, str] | None:
    """lane=, ticket= and band= (``none`` when absent) from the 3a.8 CLAIM template, or None without lane or ticket."""
    lane = re.search(r"\| lane=(\S+) \|", claim)
    ticket = re.search(r"\| ticket=(OMN-[0-9]+) \|", claim)
    band = re.search(r"\| band=(\S+) \|", claim)
    if not lane or not ticket:
        return None
    return {
        "ticket": ticket.group(1),
        "lane": lane.group(1),
        "band": band.group(1) if band else "none",
    }


def compact_stub(
    name: str, fresh: str, rules_head: str, dispatch_rules: Sequence[str]
) -> str:
    stub = COMPACT_STUBS[name].format(chars=len(fresh))
    if name != "rules":
        return stub
    # The heading and the four dispatch paragraphs stay inline: a cloud session reads them.
    heading = next((x for x in fresh.splitlines() if x.startswith(rules_head)), None)
    body = (
        fresh.split(heading + "\n", 1)[1]
        if heading and heading + "\n" in fresh
        else fresh
    )
    paragraphs = re.split(r"\n\s*\n", body)
    keep = [
        next((p for p in paragraphs if p.startswith(prefix)), None)
        for prefix in dispatch_rules
    ]
    return "\n\n".join(
        [*([heading] if heading else []), *(p.strip("\n") for p in keep if p), stub]
    )


def compact_brief(
    brief: str,
    render: Render,
    *,
    claim: str,
    names: Sequence[str],
    rules_head: str,
    dispatch_rules: Sequence[str],
) -> str:
    """Replace each NAME's exact fresh rendering in the brief by its reference; a NAME already compacted is skipped.
    ``claim`` is the brief's 3a.8 CLAIM template, whose cells parameterise the delegation and heavy-work renderings."""
    cells = compact_cells(claim)
    for name in names:
        if re.search(
            rf"^<!-- compact-ref begin {re.escape(name)} ", brief, re.MULTILINE
        ):
            continue
        if COMPACT_PARAMS[name] and cells is None:
            raise CompactRefusedError(
                f"compact: {name} needs the 3a.8 CLAIM template's lane= and ticket= cells; "
                "regenerate the 3a block with `lane_brief.py constraints`"
            )
        fresh = render(name, cells or {})
        at = brief.find(fresh)
        if at < 0:
            source = (
                "paste the committed block exactly as scripts/lane_rules_block.py prints it"
                if name == "rules"
                else "regenerate the 3a block with `lane_brief.py constraints`"
            )
            raise CompactRefusedError(
                f"compact: the fresh {name} rendering is not in the brief verbatim; {source}"
            )
        params = (
            "".join(f" {key}={cells[key]}" for key in COMPACT_PARAMS[name])
            if cells
            else ""
        )
        digest = hashlib.sha256(fresh.encode("utf-8")).hexdigest()
        block = (
            f"<!-- compact-ref begin {name} sha256={digest} chars={len(fresh)}{params} -->\n"
            f"{compact_stub(name, fresh, rules_head, dispatch_rules)}\n<!-- compact-ref end {name} -->"
        )
        brief = brief[:at] + block + brief[at + len(fresh) :]
    return brief


def expand_brief(brief: str, render: Render, verify: bool = True) -> str:
    """The exact inverse of compact_brief. With verify, a reference whose recorded sha256 differs from the fresh
    rendering is refused as stale; an unknown NAME, an unpaired marker or a missing parameter is always refused."""
    marks = iter(COMPACT_LINE_RE.finditer(brief))
    out: list[str] = []
    pos = 0
    for begin in marks:
        line = begin.group(0).rstrip("\n")
        m = COMPACT_MARK_RE.fullmatch(line)
        if not m or m.group(1) != "begin":
            raise CompactRefusedError(
                f"compact-ref: unpaired or malformed marker {line!r}; regenerate the brief"
            )
        name = m.group(2)
        if name not in COMPACT_PARAMS:
            raise CompactRefusedError(
                f"compact-ref: unknown reference {name!r}; regenerate the brief"
            )
        end = next(marks, None)
        e = COMPACT_MARK_RE.fullmatch(end.group(0).rstrip("\n")) if end else None
        if not end or not e or e.group(1) != "end" or e.group(2) != name or e.group(3):
            raise CompactRefusedError(
                f"compact-ref: the {name} reference has no matching end marker (unpaired); "
                "regenerate the brief"
            )
        cells = dict(
            cell.split("=", 1) for cell in (m.group(3) or "").split() if "=" in cell
        )
        lacking = [
            key
            for key in ("sha256", "chars", *COMPACT_PARAMS[name])
            if not cells.get(key)
        ]
        if lacking:
            raise CompactRefusedError(
                f"compact-ref: the {name} reference lacks {', '.join(lacking)}; regenerate the brief"
            )
        fresh = render(name, cells)
        if (
            verify
            and hashlib.sha256(fresh.encode("utf-8")).hexdigest() != cells["sha256"]
        ):
            raise CompactRefusedError(
                f"compact-ref: the {name} reference is stale (its sha256 differs from the fresh "
                "rendering); regenerate the brief"
            )
        out += [brief[pos : begin.start()], fresh]
        pos = end.start() + len(end.group(0).rstrip("\n"))
    return "".join(out) + brief[pos:]


def cloud_fit_problem(size: int) -> str | None:
    """Why a brief of SIZE characters cannot ride in a cloud prompt (lane-brief's ``check --cloud``), or None."""
    limit = CLOUD_PROMPT_MAX - CLOUD_HEAD_RESERVE
    if size <= limit:
        return None
    return (
        f"the brief is {size} characters, {size - limit} over the cloud brief limit of {limit} "
        f"(the {CLOUD_PROMPT_MAX}-character cloud prompt cap less {CLOUD_HEAD_RESERVE} for the runner's preamble); "
        "run `lane_brief.py compact` or place the lane on the lab"
    )


def cloud_prompt(
    brief: str,
    *,
    lane: str,
    ticket: str,
    owner: str,
    repo: str,
    landing_branch: str,
    branch: str,
    pr_heads: Sequence[Mapping[str, str]],
    run_id: str,
    timeout_min: int,
) -> str:
    """The brief, whole, after the cloud session's own rules: what the sandbox lacks and the PR it must leave.
    Raises CloudPromptTooLargeError when the two are over CLOUD_PROMPT_MAX (FRICTION 2026-10-21T22:17:51Z: a cut brief
    lost its Report section and ticket text, silently)."""
    if pr_heads:
        work = (
            f"- You work the existing pull request(s) {', '.join(h['pr'] for h in pr_heads)}: check out "
            + ", ".join(f"{h['head_ref']} (head {h['head_sha']})" for h in pr_heads)
            + f", bring it up to date with {landing_branch} and resolve what the brief names, then push to that "
            "same head branch. Do not open a second pull request."
        )
    else:
        work = (
            f"- Push your change to the branch {branch} and open one pull request from it against "
            f"{landing_branch} through GitHub from this session, titled with {ticket} and only {ticket}, its body "
            f"ending with the line `Closes {ticket}`."
        )
    head = "\n".join(
        [
            (
                f"[onex-cloud-lane] You are lane {lane}, run {run_id}, running as a Claude Code cloud session in Anthropic's "
                f"sandbox (placement=cloud, OMN-17427) with a GitHub checkout of {owner}/{repo}."
            ),
            (
                "- This session has nothing of the lab: no tailnet, lab bus, ledger, database, lab models, onex scm runtime, "
                "pr_state_local, lab-run or onex delegate. Skip every step of the brief below that needs one of them (ledger "
                "rows, inbox checks, delegation, lab proofs, pr-handoff): the launching host writes this lane's CLAIM and "
                "TERMINAL and hands its pull request to the landing controller."
            ),
            (
                "- GitHub writes go through this session's GitHub connection (git push; the pull request is opened through "
                "GitHub from this session). Never merge, arm, enable auto-merge, close or retarget a pull request."
            ),
            work,
            (
                "- Run the focused tests of your change in this sandbox and name the command and what it printed in the pull "
                "request body (a line starting `Cloud:`) and in your final message."
            ),
            f"- Stop at {max(10, timeout_min - 15)} minutes: push what you have and report it as partial.",
            (
                "- End your final message with these lines, each on its own line: "
                f"`PR: https://github.com/{owner}/{repo}/pull/<n> head=<full head sha>` (one per pull request you pushed), "
                "`LANE_RESULT outcome=<handed-off|partial|blocked>`."
            ),
            (
                "- Blocks between `<!-- compact-ref begin NAME ... -->` and `<!-- compact-ref end NAME -->` stand for "
                "lab-lane text this session cannot use: skip them. Nothing else in the brief was cut."
            ),
            "",
            "The brief follows.",
            "",
        ]
    )
    size = len(head) + len(brief)
    if size > CLOUD_PROMPT_MAX:
        raise CloudPromptTooLargeError(
            f"{CLOUD_TOO_LARGE_LEAD}{size} characters, {size - CLOUD_PROMPT_MAX} over the {CLOUD_PROMPT_MAX}-character "
            f"cap (preamble {len(head)}, brief {len(brief)}); compact the brief (python3 "
            '"$OMNI_PLUGIN_ROOT/skills/lane-brief/scripts/lane_brief.py" compact <brief> --out <file>) or place the '
            "lane on the lab (--placement lab)"
        )
    return head + brief


class HandlerBriefCompact:
    def handle(self, request: ModelBriefCompactRequest) -> ModelBriefCompactResult:
        if request.operation == "compact":
            brief = compact_brief(
                request.brief,
                request.render,
                claim=request.claim,
                names=request.names,
                rules_head=request.rules_head,
                dispatch_rules=request.dispatch_rules,
            )
        elif request.operation == "expand":
            brief = expand_brief(request.brief, request.render, verify=request.verify)
        else:
            raise ValueError(
                f"unknown operation {request.operation!r}; use compact or expand"
            )
        return ModelBriefCompactResult(brief=brief)
