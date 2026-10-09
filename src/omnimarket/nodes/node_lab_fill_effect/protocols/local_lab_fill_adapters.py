# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The local adapters of the lab-fill effect ports (OMN-20668).

They reach what the lab-fill workflow reached, the same way: the remote-lane
runner's pool read and launcher (loaded from the installed omni plugin's skills
directory, ``ONEX_OMNI_SKILLS_DIR`` or the newest installed copy), the rolling
ledger file, the approved-work list, Linear (``LINEAR_API_KEY``), the PR watcher's
state file and ``onex-triage-fences``. Each failure is a ``LabFillPortError`` that
names what failed; a reading that cannot be made is never an empty answer.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

from omnimarket.config.service_endpoints import LINEAR_GRAPHQL_URL
from omnimarket.models.work_ledger_append import (
    ModelWorkLedgerAppendReceipt,
    ModelWorkLedgerAppendRequest,
)

from .protocol_lab_fill_effect import (
    LabFillClaimFacts,
    LabFillCommandOutcome,
    LabFillPortError,
    ProtocolLabFillStatusAppender,
)

SKILLS_ENV = "ONEX_OMNI_SKILLS_DIR"
LEDGER_BUS_LANE_ENV = "ONEX_LAB_FILL_LEDGER_BUS_LANE"
WORKSPACE_ROOT_ENV = "OMNIBASE_PATH"
APPROVED_KINDS = ("process-fix", "partial-node", "wiring")
_REQUIRED = (
    "remote-lane/scripts/onex_remote_lane.py",
    "merge-drain/scripts/landing_placement.py",
)
_VERSION = re.compile(r"\d+")


def _version_key(path: Path) -> tuple[int, ...]:
    return tuple(int(n) for n in _VERSION.findall(path.parent.name))


def resolve_skills_dir(env: Mapping[str, str] | None = None) -> Path:
    """The installed omni plugin's skills directory that holds the remote-lane runner."""
    env = os.environ if env is None else env
    declared = env.get(SKILLS_ENV, "").strip()
    if declared:
        found = [Path(os.path.expandvars(declared))]
    else:
        base = Path(env.get("HOME", str(Path.home())))
        found = sorted(
            base.glob(".claude/plugins/cache/omninode-internal/omni/*/skills"),
            key=_version_key,
            reverse=True,
        )
    for skills in found:
        if all((skills / rel).is_file() for rel in _REQUIRED):
            return skills
    raise LabFillPortError(
        f"no omni plugin skills directory with the remote-lane runner (set {SKILLS_ENV})"
    )


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _load(skills: Path, name: str, rel: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, skills / rel)
    if spec is None or spec.loader is None:
        raise LabFillPortError(f"cannot load {rel}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise LabFillPortError(
            f"cannot load {rel}: {type(exc).__name__}: {exc}"
        ) from exc
    return module


class LocalPlacementReader:
    """The remote-lane runner's own pool read: hosts, admission, lane slots, running lanes."""

    def __init__(self, skills_dir: Path | None = None) -> None:
        self._skills = skills_dir

    def _dir(self) -> Path:
        return self._skills if self._skills is not None else resolve_skills_dir()

    def read_pool(self) -> Sequence[Mapping[str, object]]:
        skills = self._dir()
        try:
            lp = _load(
                skills, "landing_placement", "merge-drain/scripts/landing_placement.py"
            )
            cp = _load(
                skills, "codex_placement", "merge-drain/scripts/codex_placement.py"
            )
            pdir = lp.placement_dir()
            limited = lp.limited_hosts(pdir)
            readings = lp.read_pool(
                lp.load_pool(),
                placed=lp.placed_counts(pdir),
                lane="lab-fill placement-read",
                limited=limited,
            )
            codex = cp.codex_hosts(os.environ)
            root = Path(
                os.environ.get("ONEX_REMOTE_LANE_DIR")
                or Path.home() / ".local/state/omni/remote-lanes"
            )
            running: dict[str, list[str]] = {}
            for receipt in root.glob("*/*.json"):
                try:
                    data = json.loads(receipt.read_text(encoding="utf-8"))
                    host = data.get("host")
                    if (
                        host
                        and not data.get("final")
                        and data.get("status") in ("preparing", "running")
                        and (not data.get("pid") or _pid_alive(int(data["pid"])))
                    ):
                        running.setdefault(host, []).append(
                            data.get("lane") or receipt.parent.name
                        )
                except (OSError, ValueError, TypeError):
                    continue
            return [
                {
                    "name": r.host.name,
                    "local": r.host.local,
                    "error": r.error,
                    "cores": r.cores,
                    "load1": r.load1,
                    "busy_cores": r.busy_cores,
                    "mem_avail_gb": round(r.mem_avail_gb, 1),
                    "placed": r.placed,
                    "lane_cap": r.lane_cap,
                    "runner_slots": r.lane_slots,
                    "refusal": r.lane_admission_refusal,
                    "login_claude": "claude" in r.host.engines,
                    "codex_ok": r.host.name in codex,
                    "limited": limited.get(r.host.name),
                    "running_lanes": running.get(r.host.name, []),
                    "describe": r.describe(),
                }
                for r in readings
            ]
        except LabFillPortError:
            raise
        except Exception as exc:
            raise LabFillPortError(
                f"pool read failed: {type(exc).__name__}: {exc}"
            ) from exc

    def limited_hosts(self) -> frozenset[str]:
        skills = self._dir()
        try:
            lp = _load(
                skills, "landing_placement", "merge-drain/scripts/landing_placement.py"
            )
            return frozenset(lp.limited_hosts(lp.placement_dir()))
        except LabFillPortError:
            raise
        except Exception as exc:
            raise LabFillPortError(
                f"limited markers: {type(exc).__name__}: {exc}"
            ) from exc


class LocalLedgerReader:
    """The rolling ledger file: this fire's STATUS row, if one is there."""

    def status_row_time(self, ledger_path: str, run_key: str) -> str | None:
        path = Path(os.path.expandvars(ledger_path))
        try:
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    cells = [c.strip() for c in line.split("|")]
                    if (
                        len(cells) > 2
                        and cells[1] == "STATUS"
                        and "lane=lab-fill" in cells
                        and f"run={run_key}" in cells
                    ):
                        return cells[0]
        except OSError as exc:
            raise LabFillPortError(f"ledger unreadable: {exc}") from exc
        return None


class LocalApprovedWork:
    """The declared approved-work list (version 1): its depth, and one row by id."""

    @staticmethod
    def _load(path: str) -> dict[str, object]:
        try:
            value = json.loads(
                Path(os.path.expandvars(path)).read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as exc:
            raise LabFillPortError(
                f"unreadable: {(str(exc).splitlines() or [type(exc).__name__])[0]}"
            ) from exc
        if (
            not isinstance(value, dict)
            or value.get("version") != 1
            or not isinstance(value.get("rows"), list)
        ):
            raise LabFillPortError("invalid list")
        return value

    def depth(self, path: str) -> int | None:
        try:
            rows = self._load(path)["rows"]
        except LabFillPortError:
            return None
        return len(rows) if isinstance(rows, list) else None

    def row(self, path: str, row_id: str) -> Mapping[str, object]:
        rows = self._load(path)["rows"]
        matches = [
            r
            for r in (rows if isinstance(rows, list) else [])
            if isinstance(r, dict) and r.get("id") == row_id
        ]
        if len(matches) != 1:
            raise LabFillPortError(f"APPROVED-ROW {row_id} missing or duplicate row")
        return matches[0]


def _graphql(query: str, timeout_s: float = 20.0) -> dict[str, object]:
    key = os.environ.get("LINEAR_API_KEY", "")
    if not key:
        raise LabFillPortError("LINEAR_API_KEY is missing")
    req = urllib.request.Request(
        LINEAR_GRAPHQL_URL,
        data=json.dumps({"query": query}).encode(),
        headers={"Authorization": key, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as response:
            result = json.load(response)
    except (OSError, ValueError, urllib.error.URLError) as exc:
        raise LabFillPortError(f"Linear unreadable: {type(exc).__name__}") from exc
    if result.get("errors"):
        raise LabFillPortError("Linear GraphQL errors")
    data = result.get("data")
    if not isinstance(data, dict):
        raise LabFillPortError("Linear returned no data")
    return data


def _watcher_path() -> Path:
    declared = os.environ.get("ONEX_PR_WATCHER_STATE")
    if declared:
        return Path(declared)
    return Path(os.environ["OMNI_HOME"]) / ".claude_scratch/pr-watcher/state.json"


class LocalLiveChecks:
    """Assignment, fences, PR hold labels and completion for one lane, read right before launch."""

    def skip_reason(
        self, *, ticket: str, kind: str, pr: str, operator_id: str, ledger_path: str
    ) -> tuple[str, str]:
        number = int(ticket.split("-", 1)[1])
        data = _graphql(
            '{issues(filter:{team:{key:{eq:"OMN"}},number:{eq:'
            + str(number)
            + "}}){nodes{identifier state{type} assignee{id}}}}"
        )
        issues = data.get("issues")
        nodes = issues.get("nodes", []) if isinstance(issues, dict) else []
        if not nodes:
            raise LabFillPortError(f"{ticket} not found in Linear")
        node = nodes[0]
        assignee = (node.get("assignee") or {}).get("id", "")
        if assignee and assignee != operator_id:
            return "assigned-other", f"assignee {assignee}"
        if kind in APPROVED_KINDS and node["state"]["type"] in (
            "completed",
            "canceled",
        ):
            return "done", f"state {node['state']['type']}"
        try:
            fenced = self._fences(ledger_path)
        except LabFillPortError as exc:
            return "fences-unreadable", str(exc)[:200]
        if ticket in fenced:
            return "fenced", ticket
        if pr:
            return self._pr_hold(pr)
        return "", ""

    @staticmethod
    def _fences(ledger_path: str) -> set[str]:
        try:
            out = subprocess.run(
                ["onex-triage-fences", os.path.expandvars(ledger_path), "--open-prs"],
                capture_output=True,
                text=True,
                timeout=60,
                check=True,
            )
            return set(json.loads(out.stdout)["fenced_tickets"])
        except (
            OSError,
            subprocess.SubprocessError,
            ValueError,
            KeyError,
            TypeError,
        ) as exc:
            raise LabFillPortError(f"onex-triage-fences: {type(exc).__name__}") from exc

    @staticmethod
    def _pr_hold(pr: str) -> tuple[str, str]:
        repo, _, number = pr.partition("#")
        try:
            state = json.loads(_watcher_path().read_text(encoding="utf-8"))
            for record in state.get("prs", {}).values():
                facts = record.get("facts") or {}
                if (
                    str(facts.get("repo", "")).split("/")[-1] == repo
                    and str(facts.get("number")) == number
                ):
                    for label in facts.get("labels", []):
                        low = str(label).lower()
                        if low.startswith("hold:") and low != "hold:auto-merge":
                            return "held", str(label)
                    return "", ""
        except (OSError, ValueError, KeyError, AttributeError) as exc:
            return "pr-state-unreadable", type(exc).__name__
        return "pr-state-unreadable", f"{pr} not in the watcher state"


class LocalBriefBlocks:
    """The committed standing rules, then lane-brief's delegation and lab blocks."""

    def __init__(self, skills_dir: Path | None = None) -> None:
        self._skills = skills_dir

    def blocks(self, *, ticket: str, brief_path: str) -> str:
        import argparse

        skills = self._skills if self._skills is not None else resolve_skills_dir()
        rules_script = skills.parent / "scripts" / "lane_rules_block.py"
        try:
            rules = subprocess.run(
                [sys.executable, str(rules_script)],
                capture_output=True,
                text=True,
                timeout=60,
                check=True,
            ).stdout
        except (OSError, subprocess.SubprocessError) as exc:
            raise LabFillPortError(f"lane_rules_block: {type(exc).__name__}") from exc
        sys.path.insert(0, str(skills / "lane-brief/scripts"))
        try:
            lb = _load(skills, "lane_brief", "lane-brief/scripts/lane_brief.py")
            brief = lb.unfenced_prose(
                Path(brief_path).read_text(encoding="utf-8") + "\n\n" + rules
            )
            section = re.search(
                r"^" + re.escape(lb.RULES_BLOCK_HEAD) + r"[^\n]*\n(.*?)(?=^#{1,2} |\Z)",
                brief,
                re.MULTILINE | re.DOTALL,
            )
            if section is None:
                raise LabFillPortError("brief standing rules heading is missing")
            paragraphs = re.split(r"\n\s*\n", section.group(1))
            for prefix, markers in lb.DISPATCH_RULES.items():
                paragraph = next((p for p in paragraphs if p.startswith(prefix)), "")
                for marker in markers:
                    if marker not in paragraph:
                        raise LabFillPortError(
                            f"brief standing rules lack {prefix} {marker}"
                        )
            args = argparse.Namespace(ticket=ticket, band=None)
            return f"{rules}\n{lb.delegation_block(args)}\n\n{lb.lab_block(args)}\n"
        except LabFillPortError:
            raise
        except Exception as exc:
            raise LabFillPortError(f"lane_brief: {type(exc).__name__}: {exc}") from exc
        finally:
            if sys.path and sys.path[0] == str(skills / "lane-brief/scripts"):
                sys.path.pop(0)


class LocalLaneLauncher:
    """``python3 onex_remote_lane.py <args>`` from OMNI_HOME, bounded by the lane's slice."""

    def __init__(self, skills_dir: Path | None = None) -> None:
        self._skills = skills_dir

    def run(
        self, args: Sequence[str], *, env: Mapping[str, str], timeout_s: float
    ) -> LabFillCommandOutcome:
        skills = self._skills if self._skills is not None else resolve_skills_dir()
        script = skills / "remote-lane/scripts/onex_remote_lane.py"
        cwd = os.environ.get("OMNI_HOME") or None
        try:
            done = subprocess.run(
                ["python3", str(script), *args],
                capture_output=True,
                text=True,
                timeout=max(1.0, timeout_s),
                cwd=cwd,
                env={**os.environ, **env},
            )
        except subprocess.TimeoutExpired:
            return LabFillCommandOutcome(124, "", "timed out")
        except OSError as exc:
            raise LabFillPortError(f"runner not started: {exc}") from exc
        return LabFillCommandOutcome(done.returncode, done.stdout, done.stderr)


class LocalReceiptReader:
    """One runner receipt file."""

    def read(self, path: str) -> Mapping[str, object] | None:
        try:
            value = json.loads(
                Path(os.path.expandvars(path)).read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            return None
        return value if isinstance(value, dict) else None


class LocalResultWriter:
    """The fire's result file, replaced atomically."""

    def write(self, path: str, value: Mapping[str, object]) -> None:
        target = Path(os.path.expandvars(path))
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=target.parent, delete=False, suffix=".tmp"
        ) as handle:
            temporary = handle.name
            handle.write(json.dumps(value, indent=2, sort_keys=True) + "\n")
        os.replace(temporary, target)


class BusLaneLedgerAppender:
    """`onex work-ledger append`'s caller over a declared bus lane."""

    def __init__(self, lane: str, workspace_root: Path) -> None:
        self._lane = lane
        self._root = workspace_root

    async def append(
        self, request: ModelWorkLedgerAppendRequest, *, timeout_s: float
    ) -> ModelWorkLedgerAppendReceipt | None:
        from omnimarket.delegated_test_loop.lane_bus import open_lab_run_bus
        from omnimarket.work_ledger_bus.bus import WorkLedgerAppendCaller

        async with open_lab_run_bus(
            bus="kafka", lane=self._lane, kafka_bootstrap=None, omni_home=self._root
        ) as bus:
            caller = WorkLedgerAppendCaller(bus)
            try:
                return await caller.append(request, timeout_s=timeout_s)
            except TimeoutError:
                return None
            finally:
                await caller.stop()


def appender_from_environment() -> ProtocolLabFillStatusAppender | None:
    """The bus appender the deployment declares, or None when it declares none."""
    lane = os.environ.get(LEDGER_BUS_LANE_ENV, "").strip()
    root = os.environ.get(WORKSPACE_ROOT_ENV, "").strip()
    if not lane or not root:
        return None
    return BusLaneLedgerAppender(lane, Path(root))


# The claim store is read by omnibase_internal.claim_index (ledger window, archive pointers,
# staleness), which has not been promoted into this repository. Until it is, this bridge runs
# in that project's own environment and prints JSON; nothing else of omnibase_internal is used.
_CLAIM_BRIDGE = """
import json, re, sys
from datetime import datetime, timezone
from pathlib import Path
from omnibase_internal.claim_index import STALENESS_HOURS, build_index_from_sources, window_sources

ledger, tickets = Path(sys.argv[1]), json.loads(sys.argv[2])
now = datetime.now(timezone.utc)
out = {"staleness_hours": STALENESS_HOURS}

def stamp(value):
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None

def pr_key(value):
    return value.strip().lower().split("/")[-1]

def subjects_of(values):
    found = set(re.findall("OMN-[0-9]+", " ".join(values.get(k, "") for k in ("ticket", "tickets", "related"))))
    for k in ("pr", "handed-off"):
        found.update(pr_key(m) for m in re.findall("[A-Za-z0-9_./-]+#[0-9]+", values.get(k, "")))
    return found

try:
    sources = window_sources(ledger, str(ledger), now=now)
except Exception as exc:
    err = type(exc).__name__ + ":" + str(exc)
    out["index"], out["open_claims"] = err, err
else:
    try:
        index = build_index_from_sources(sources, now=now)
        out["index"] = {t: {"lane": str(index["tickets"][t]["lane"]), "state": str(index["tickets"][t].get("state", "held"))}
                        for t in tickets if t in index.get("tickets", {})}
    except Exception as exc:
        out["index"] = type(exc).__name__ + ":" + str(exc)
    open_claims = {}
    for source in sources:
        for line in source.text.splitlines():
            cells = [c.strip() for c in line.split("|")]
            if len(cells) < 3:
                continue
            when, row_class = stamp(cells[0]), cells[1]
            values = dict(c.split("=", 1) for c in cells[2:] if "=" in c)
            lane = values.get("lane", "").strip()
            if not lane or when is None:
                continue
            subjects = subjects_of(values)
            if row_class == "CLAIM":
                for s in subjects:
                    open_claims[(lane, s)] = when
            elif row_class in ("TERMINAL", "RELEASE"):
                for key in [k for k in open_claims if k[0] == lane and (not subjects or k[1] in subjects)]:
                    del open_claims[key]
            else:
                for s in subjects:
                    if (lane, s) in open_claims:
                        open_claims[(lane, s)] = when
    out["open_claims"] = [{"lane": lane, "subject": s, "when": when.strftime("%Y-%m-%dT%H:%M:%SZ")}
                          for (lane, s), when in sorted(open_claims.items())]
print(json.dumps(out))
"""
_REGISTRY_LINE = re.compile(r"\s*(\S+#[0-9]+)\s*[|]\s*lane:\s*([^|]*[^|\s])")


def _pr_key(value: str) -> str:
    return value.strip().lower().split("/")[-1]


class LocalOwnerReader:
    """The claim store (through its bridge), the PR claim registry CLI and the watcher's state file."""

    def __init__(self, obi_home: str | None = None) -> None:
        self._obi = obi_home

    def claims(self, ledger_path: str, tickets: Sequence[str]) -> LabFillClaimFacts:
        obi = self._obi or os.environ.get("OMNIBASE_INTERNAL_HOME")
        if not obi:
            home = os.environ.get("OMNI_HOME", "")
            obi = str(Path(home).parent / "omnibase_internal") if home else ""
        if not obi or not Path(obi).is_dir():
            raise LabFillPortError(
                "omnibase_internal is not found (set OMNIBASE_INTERNAL_HOME)"
            )
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        try:
            done = subprocess.run(
                [
                    "uv",
                    "run",
                    "--project",
                    obi,
                    "python",
                    "-c",
                    _CLAIM_BRIDGE,
                    os.path.expandvars(ledger_path),
                    json.dumps(list(tickets)),
                ],
                capture_output=True,
                text=True,
                timeout=60,
                env=env,
            )
            if done.returncode != 0:
                raise LabFillPortError(
                    f"claim bridge exit {done.returncode}: {done.stderr.strip()[-120:]}"
                )
            value = json.loads(done.stdout)
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            raise LabFillPortError(f"claim bridge: {type(exc).__name__}") from exc
        return LabFillClaimFacts(
            index=value["index"],
            open_claims=value["open_claims"],
            staleness_hours=float(value["staleness_hours"]),
        )

    def pr_claims(self, cli_path: str) -> Mapping[str, str]:
        try:
            done = subprocess.run(
                ["python3", os.path.expandvars(cli_path), "list"],
                capture_output=True,
                text=True,
                timeout=20,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise LabFillPortError(f"pr claim registry: {type(exc).__name__}") from exc
        if done.returncode != 0:
            raise LabFillPortError(
                f"exit {done.returncode} {done.stderr.strip()[-80:]}"
            )
        found: dict[str, str] = {}
        for line in done.stdout.splitlines():
            match = _REGISTRY_LINE.match(line)
            if match:
                found[_pr_key(match.group(1))] = match.group(2)
        return found

    def watcher_merged(self, state_path: str) -> Mapping[str, Sequence[str]]:
        path = (
            state_path
            or os.environ.get("ONEX_PR_WATCHER_STATE")
            or str(_watcher_path())
        )
        try:
            state = json.loads(Path(path).read_text(encoding="utf-8"))
            tick = datetime.fromisoformat(
                str(state["last_tick"]).replace("Z", "+00:00")
            )
            age_min = (datetime.now(UTC) - tick).total_seconds() / 60
            if state.get("schema") != 1 or not 0 <= age_min <= 8:
                raise LabFillPortError("unreadable-from-snapshot")
            records = [
                r.get("facts") or {} for r in state.get("prs", {}).values()
            ] + list(state.get("merges", {}).values())
        except LabFillPortError:
            raise
        except (OSError, ValueError, KeyError, AttributeError, TypeError) as exc:
            raise LabFillPortError(f"{type(exc).__name__}") from exc
        merged: dict[str, set[str]] = {}
        for facts in records:
            repo = str(facts.get("repo", "")).split("/")[-1].lower()
            title = str(facts.get("title", ""))
            is_merged = facts.get("state") == "MERGED" or facts.get("merged_at")
            if (
                not is_merged
                or repo == "onex_change_control"
                or title.lower().startswith("evidence(")
            ):
                continue
            for ticket in re.findall("OMN-[0-9]+", title):
                merged.setdefault(ticket, set()).add(
                    f"{repo}#{facts.get('number', '')}"
                )
        return {t: sorted(names) for t, names in merged.items()}
