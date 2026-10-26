# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Budgeted gateway reads and host-to-launcher accounting for remote lanes."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shlex
import signal
import subprocess
import tempfile
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from omnimarket.handlers.overlay_keys import resolve_overlay_key

from ..models.model_lane_github_budget import ModelLaneGithubCall, ModelLaneGithubResult

POLICY = json.loads(
    (Path(__file__).resolve().parents[1] / "contract.yaml").read_text()
)["policy"]

READ_GUIDANCE = (
    "Use pr_state_local.py for PR state and check runs; use onex scm job-log or run-view "
    "for budgeted, shared CI reads through node_github_repo_gateway_effect."
)
LOCAL_SCM = frozenset(
    {"pr-view", "pr-status", "pr-checks", "pr-list", "pr-merge-commit", "pr-heads"}
)
GATEWAY_SCM = frozenset(
    {
        "pr-review",
        "pr-ticket",
        "branch-protection",
        "run-list",
        "run-view",
        "job-log",
        "run-download",
        "release-list",
        "pr-merged",
    }
)
SAFE = re.compile(r"[^A-Za-z0-9._/:@+=, -]")
TOKEN = re.compile(r"(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]+")


def clean(value: str) -> str:
    return SAFE.sub("", TOKEN.sub("redacted", value))[:240]


def gh_kind(args: tuple[str, ...]) -> tuple[str, str, str]:
    """Conservative classification: only explicit mutations, local credential lookup and the
    login probe pass. The probe is the session preflight's check (OMN-17427): refused as a read,
    every lane reported its own refusal as a failed login. A token print is never a probe."""
    pair = args[:2]
    cls = " ".join(pair)
    if pair in {("auth", "git-credential"), ("auth", "token")}:
        return cls, "LOCAL", "-"
    if pair == ("auth", "status") and not {"-t", "--show-token"} & set(args):
        return cls, "AUTH", "-"
    if pair in {
        ("pr", verb)
        for verb in ("create", "edit", "ready", "close", "reopen", "comment", "merge")
    } | {("workflow", "run"), ("run", "rerun"), ("run", "cancel")}:
        return cls, "WRITE", "-"
    if args and args[0] == "api":
        method, endpoint, query = "GET", "-", ""
        i = 1
        while i < len(args):
            arg = args[i]
            if arg in {
                "-X",
                "--method",
                "-f",
                "-F",
                "--field",
                "--raw-field",
                "--input",
            }:
                i += 1
                value = args[i] if i < len(args) else ""
                if arg in {"-X", "--method"}:
                    method = value.upper()
                elif arg != "--input":
                    if value.startswith("query="):
                        query = value[6:]
                    elif method == "GET":
                        method = "POST"
            elif arg.startswith("--method="):
                method = arg.partition("=")[2].upper()
            elif not arg.startswith("-") and endpoint == "-":
                endpoint = arg.split("?", 1)[0]
            i += 1
        if endpoint == "graphql":
            # A query sent with POST still reads. Never record query text.
            query = re.sub(r"(?m)^\s*#.*$", "", query).lstrip()
            return (
                "api graphql",
                "WRITE" if re.match(r"mutation\b", query) else "GET",
                "graphql",
            )
        return "api", "WRITE" if method not in {"GET", "HEAD"} else method, endpoint
    return cls or "unknown", "GET", "-"


class HandlerLaneGithubBudget:
    def handle(self, request: ModelLaneGithubCall) -> ModelLaneGithubResult:
        policy, tool, args = request.policy, request.tool, request.args
        lane = str(policy["lane"])
        if tool not in {"gh", "onex"}:
            raise ValueError("unknown tool")
        budget = policy["budget"]
        if (
            type(budget) is not int
            or not 0 <= budget <= POLICY["gateway_cache_misses_per_lane"]
        ):
            raise ValueError("invalid lane read budget")
        if tool == "gh":
            cls, method, endpoint = gh_kind(args)
            if method == "AUTH":
                return self.auth_status(request, cls, endpoint)
            if method not in {"LOCAL", "WRITE"}:
                result = ModelLaneGithubResult(
                    78,
                    stderr=f"REFUSED lane={lane} raw GitHub read budget=0. {READ_GUIDANCE}\n",
                )
                self.record(policy, cls, method, endpoint, result.code, "refused")
                return result
            self.record(policy, cls, method, endpoint, -1, "allowed")
            return self.execute(request)
        if len(args) >= 2 and args[0] == "scm" and args[1] in LOCAL_SCM:
            return self.execute(replace(request, args=(*args, "--source", "local")))
        if args[:2] == ("scm", "commit-check-runs"):
            result = ModelLaneGithubResult(
                78,
                stderr=f"REFUSED lane={lane}: check runs come from pr_state_local.py, never per-lane REST pagination.\n",
            )
            self.record(policy, "scm commit-check-runs", "GET", "-", 78, "refused")
            return result
        if len(args) < 2 or args[0] != "scm" or args[1] not in GATEWAY_SCM:
            return self.execute(request, capture=False)
        cls, endpoint = f"scm {args[1]}", "/".join(args[2:3])
        try:
            return self.cached(request, cls, endpoint)
        except (OSError, ValueError) as exc:
            result = ModelLaneGithubResult(
                78,
                stderr=f"REFUSED lane={lane} GitHub read budget={budget}: accounting/cache unavailable ({type(exc).__name__}).\n",
            )
            self.record(policy, cls, "GET", endpoint, result.code, "refused")
            return result

    @staticmethod
    def execute(
        request: ModelLaneGithubCall,
        *,
        capture: bool = True,
        timeout: float | None = None,
    ) -> ModelLaneGithubResult:
        # Preserve the host shim's guards. This boundary records the invocation once;
        # the nested shim must not count it again in the host JSON/TSV logs.
        env = {**request.env, "ONEX_LANE": str(request.policy["lane"])}
        if request.tool == "gh":
            env.update(ONEX_GH_CALLS_LOG=os.devnull, ONEX_GH_USAGE_LOG="0")
        if request.tool in {"gh", "onex"}:
            # The host gh shim walks PATH for "the real gh" and takes the first one that is not
            # itself. With this lane's wrapper directory still on PATH that is the wrapper:
            # wrapper -> shim -> wrapper, and an AUTH probe then waits on the lock its own parent
            # holds (OMN-17427, measured on a lab host 2026-10-18).
            # The onex shim forwards to the sanctioned wrapper too, which warned that the shim
            # shadows the CLI venv on every call (OMN-21396).
            wrappers = Path(
                request.policy.get("bin")
                or Path(request.policy["log"]).parent / "github-bin"
            ).resolve()
            env["PATH"] = os.pathsep.join(
                entry
                for entry in env.get("PATH", "").split(os.pathsep)
                if entry and Path(entry).resolve() != wrappers
            )
        command = [str(request.policy[request.tool]), *request.args]
        if timeout is None:
            done = subprocess.run(
                command, env=env, capture_output=capture, text=True, check=False
            )
            return ModelLaneGithubResult(
                done.returncode, done.stdout or "", done.stderr or ""
            )
        # A bounded call owns its process group: the host shim forks, and a grandchild holding the
        # pipe would otherwise keep the timeout waiting for the output of a process it killed.
        child = subprocess.Popen(
            command,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            stdout, stderr = child.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGKILL)
            child.communicate()
            raise
        return ModelLaneGithubResult(child.returncode, stdout or "", stderr or "")

    @staticmethod
    def record(
        policy: Mapping[str, Any],
        cls: str,
        method: str,
        endpoint: str,
        code: int,
        action: str,
    ) -> None:
        path = Path(policy["log"])
        path.parent.mkdir(parents=True, exist_ok=True)
        fields = (
            datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            str(policy["lane"]),
            "env",
            cls,
            method,
            endpoint,
            str(code),
            action,
        )
        with path.open("a", encoding="utf-8") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            stream.write("\t".join(clean(value) for value in fields) + "\n")

    def cached(
        self,
        request: ModelLaneGithubCall,
        cls: str,
        endpoint: str,
    ) -> ModelLaneGithubResult:
        policy = request.policy
        root = Path(policy["cache"])
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        key = hashlib.sha256(json.dumps(request.args).encode()).hexdigest()
        cache = root / f"{key}.json"
        ttl = (
            POLICY["job_log_ttl_seconds"]
            if request.args[1] == "job-log"
            else POLICY["run_view_ttl_seconds"]
        )
        # One host-wide key lock: two lanes missing the same job make one gateway request.
        with (root / f"{key}.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if cache.exists() and 0 <= time.time() - cache.stat().st_mtime < ttl:
                data = json.loads(cache.read_text())
                result = ModelLaneGithubResult(0, data["stdout"], data["stderr"])
                self.record(policy, cls, "GET", endpoint, 0, "cache")
                return result
            spent = Path(policy["spent"])
            spent.parent.mkdir(parents=True, exist_ok=True)
            with spent.open("a+") as stream:
                fcntl.flock(stream, fcntl.LOCK_EX)
                stream.seek(0)
                used = int(stream.read() or "0")
                if used < 0 or used >= policy["budget"]:
                    result = ModelLaneGithubResult(
                        78,
                        stderr=f"REFUSED lane={policy['lane']} GitHub read budget={policy['budget']} exhausted; shared cache hits remain available.\n",
                    )
                    self.record(policy, cls, "GET", endpoint, result.code, "refused")
                    return result
                stream.seek(0)
                stream.truncate()
                stream.write(str(used + 1))
                stream.flush()
                os.fsync(stream.fileno())
            self.record(policy, cls, "GET", endpoint, -1, "allowed")
            return self.store(cache, self.execute(request))

    def auth_status(
        self, request: ModelLaneGithubCall, cls: str, endpoint: str
    ) -> ModelLaneGithubResult:
        """The session preflight's login probe: host-shared, never the lane's read budget, and
        bounded so it cannot stall a lane (OMN-17427). A fresh answer is served without taking the
        lock; a miss takes it for a bounded wait and runs the host's check for a bounded time; a
        failed login (nonzero exit) is never cached, so it is always the host's real answer."""
        policy = request.policy
        cache = Path(policy["cache"]) / (
            hashlib.sha256(json.dumps(request.args).encode()).hexdigest() + ".json"
        )
        fresh = self.cached_answer(cache, POLICY["auth_status_ttl_seconds"])
        if fresh:
            self.record(policy, cls, "AUTH", endpoint, 0, "cache")
            return fresh
        bound = POLICY["auth_status_timeout_seconds"]
        try:
            with self.bounded_lock(cache.with_suffix(".lock")):
                fresh = self.cached_answer(cache, POLICY["auth_status_ttl_seconds"])
                if fresh:
                    self.record(policy, cls, "AUTH", endpoint, 0, "cache")
                    return fresh
                self.record(policy, cls, "AUTH", endpoint, -1, "allowed")
                answer = self.execute(request, timeout=bound)
                with suppress(OSError):
                    self.store(cache, answer)
                return answer
        except (TimeoutError, subprocess.TimeoutExpired):
            reason = f"the host check did not answer within {bound}s"
        except (OSError, ValueError):
            # The shared cache is unusable here (a read-only state directory): ask the host uncached.
            self.record(policy, cls, "AUTH", endpoint, -1, "allowed")
            try:
                return self.execute(request, timeout=bound)
            except subprocess.TimeoutExpired:
                reason = f"the host check did not answer within {bound}s"
        return self.degraded_auth(request, cls, endpoint, cache, reason)

    @staticmethod
    @contextmanager
    def bounded_lock(path: Path) -> Iterator[None]:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        deadline = time.monotonic() + POLICY["auth_status_lock_wait_seconds"]
        with path.open("a") as lock:
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("the login probe lock is held") from None
                    time.sleep(0.05)
            yield

    @staticmethod
    def cached_answer(cache: Path, max_age: float) -> ModelLaneGithubResult | None:
        try:
            age = time.time() - cache.stat().st_mtime
            if not 0 <= age < max_age:
                return None
            data = json.loads(cache.read_text())
            return ModelLaneGithubResult(0, data["stdout"], data["stderr"])
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def degraded_auth(
        self,
        request: ModelLaneGithubCall,
        cls: str,
        endpoint: str,
        cache: Path,
        reason: str,
    ) -> ModelLaneGithubResult:
        """The host check stalled. Answer from what is already known on this host, say so on
        stderr, and pass a lane that writes to GitHub only on an answer the host verified."""
        policy = request.policy
        lane = str(policy["lane"])
        known = self.cached_answer(cache, POLICY["auth_status_stale_seconds"])
        if known:
            self.record(policy, cls, "AUTH", endpoint, 0, "stale")
            return ModelLaneGithubResult(
                0,
                known.stdout,
                f"WARN lane={lane}: {reason}; this is the host's last verified login answer.\n",
            )
        try:
            local = self.execute(
                replace(request, args=("auth", "token")),
                timeout=POLICY["auth_status_local_seconds"],
            )
            held = local.code == 0 and bool(local.stdout.strip())
        except subprocess.TimeoutExpired:
            held = False  # the token itself is never printed or kept: only its presence is read
        if not held:
            self.record(policy, cls, "AUTH", endpoint, 1, "refused")
            return ModelLaneGithubResult(
                1,
                stderr=f"REFUSED lane={lane}: {reason}, and no local credential was found. "
                "Log the host in once on the host itself; a lane never logs in.\n",
            )
        if request.env.get("ONEX_LANE_GITHUB_WRITES") == "0":
            self.record(policy, cls, "AUTH", endpoint, 0, "degraded")
            return ModelLaneGithubResult(
                0,
                "github.com\n  Logged in to github.com (local credential present; GitHub was not asked)\n",
                f"WARN lane={lane}: {reason}; the login is not verified. Allowed because this lane "
                "declares it writes nothing to GitHub (ONEX_LANE_GITHUB_WRITES=0).\n",
            )
        self.record(policy, cls, "AUTH", endpoint, 75, "refused")
        return ModelLaneGithubResult(
            75,
            stderr=f"REFUSED lane={lane}: {reason}; the login is not verified and this lane "
            "writes to GitHub. Retry; a read-only lane declares ONEX_LANE_GITHUB_WRITES=0.\n",
        )

    @staticmethod
    def store(cache: Path, result: ModelLaneGithubResult) -> ModelLaneGithubResult:
        if result.code == 0:
            temporary = cache.with_suffix(f".{os.getpid()}.tmp")
            temporary.write_text(
                json.dumps({"stdout": result.stdout, "stderr": result.stderr})
            )
            temporary.chmod(0o600)
            temporary.replace(cache)
        return result


def setup_lines() -> list[str]:
    """Install per-run wrappers, retaining each host's own tools and no credentials."""
    setup = """import json, os, pathlib, shlex, shutil, sys
c = pathlib.Path(os.environ["C"])
search = os.pathsep.join(p for p in os.environ["PATH"].split(os.pathsep) if p and pathlib.Path(p).resolve() != (c/"github-bin").resolve())
policy = {"lane": os.environ["ONEX_LANE"], "host": os.environ["ONEX_LANE_HOST"], "budget": int(sys.argv[1]),
          "gh": shutil.which("gh", path=search), "onex": shutil.which("onex", path=search) or str(pathlib.Path(os.environ["ONEX_REGISTRY_ROOT"])/sys.argv[2]/"scripts/onex"),
          "bin": str(c/"github-bin"), "log": str(c/"gh-calls.log"), "spent": str(c/"github-read-spent"),
          "cache": str(pathlib.Path(os.environ["ONEX_STATE_DIR"])/"github-read-cache")}
if not policy["gh"]: raise SystemExit("GitHub lane policy: host gh unavailable")
config = c/"github-policy.json"
temporary = config.with_suffix(".tmp")
temporary.write_text(json.dumps(policy)); temporary.chmod(0o444); temporary.replace(config)
for tool in ("gh", "onex"):
    wrapper = c/"github-bin"/tool
    wrapper.parent.mkdir(exist_ok=True)
    wrapper.write_text("#!/bin/sh\\nexec " + shlex.quote(sys.executable) +
        " -c "+shlex.quote("from omnimarket.nodes.node_lane_github_budget_effect import main; raise SystemExit(main())")+" " + shlex.quote(str(config)) + " " + tool + ' "$@"\\n')
    wrapper.chmod(0o755)
"""
    return [
        "export C",
        f"python3 -c {shlex.quote(setup)} {POLICY['gateway_cache_misses_per_lane']} {shlex.quote(resolve_overlay_key('repos.deploy'))} || exit 78",
        'export PATH="$C/github-bin:$PATH"',
    ]


def call_log_path(env: Mapping[str, str]) -> Path:
    if env.get("ONEX_GH_CALLS_LOG"):
        return Path(env["ONEX_GH_CALLS_LOG"])
    if env.get("ONEX_STATE_DIR"):
        return Path(env["ONEX_STATE_DIR"]) / "gh-calls.log"
    if env.get("ONEX_REGISTRY_ROOT"):
        return Path(env["ONEX_REGISTRY_ROOT"]) / ".onex_state" / "gh-calls.log"
    # A refused relay, never a lane crash: refresh_scope reports it and the lane runs on.
    raise ValueError(
        "GitHub accounting has no call log: ONEX_GH_CALLS_LOG, ONEX_STATE_DIR and ONEX_REGISTRY_ROOT are unset"
    )


def quota_consumers(
    log_path: Path,
    remaining: int | None,
    now: datetime,
    limit: int = 5000,
    window: timedelta = timedelta(minutes=60),
) -> dict[str, Any]:
    """Host log plus idempotently replaced remote snapshots; no local/cache/refused calls."""
    result: dict[str, Any] = {
        "window_min": int(window.total_seconds() / 60),
        "logged": 0,
        "unlogged": max(0, limit - remaining) if remaining is not None else None,
        "top": [],
        "top_consumer": "unread",
    }
    counts: Counter[str] = Counter()
    readable = False
    for path in [log_path, *sorted((log_path.parent / "gh-calls.d").glob("*.log"))]:
        try:
            with path.open("rb") as stream:
                stream.seek(0, os.SEEK_END)
                start = max(0, stream.tell() - 4 * 1024 * 1024)
                stream.seek(start)
                lines = (
                    stream.read(4 * 1024 * 1024).decode("utf-8", "replace").splitlines()
                )
                readable = True
        except OSError:
            continue
        for line in lines[1:] if start else lines:
            cells = line.split("\t")
            if (
                len(cells) != 8
                or cells[7] not in {"allowed", "fallback"}
                or cells[4] == "LOCAL"
                or not cells[1]
            ):
                continue
            try:
                at = datetime.strptime(cells[0], "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=UTC
                )
            except ValueError:
                continue
            if now - window <= at <= now:
                counts[cells[1]] += 1
    if not readable:
        return result
    logged = sum(counts.values())
    top = [
        [lane, count]
        for lane, count in sorted(counts.items(), key=lambda row: (-row[1], row[0]))[:3]
    ]
    unlogged = max(0, limit - remaining - logged) if remaining is not None else None
    if unlogged is not None and unlogged > max(counts.values(), default=0):
        result["top_consumer"] = f"unlogged n={unlogged}"
    elif top:
        result["top_consumer"] = f"{top[0][0]} n={top[0][1]}"
    result.update(logged=logged, unlogged=unlogged, top=top)
    return result


def bounded_lines(path: Path) -> list[str]:
    try:
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            start = max(0, stream.tell() - 4 * 1024 * 1024)
            stream.seek(start)
            lines = stream.read(4 * 1024 * 1024).decode("utf-8", "replace").splitlines()
        return lines[1:] if start else lines
    except FileNotFoundError:
        return []


def host_call_snapshot(
    run_id: str,
    include_host: bool,
    *,
    home: Path | None = None,
    cache_home: Path | None = None,
    now: datetime | None = None,
) -> dict[str, str]:
    """Read existing sanitized shim JSON usage, including watcher calls with no TSV destination."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", run_id):
        raise ValueError("invalid run id")
    home, now = home or Path.home(), now or datetime.now(UTC)
    cache_home = cache_home or Path(
        os.environ.get("XDG_CACHE_HOME", str(home / ".cache"))
    )
    result = {}
    lane_log = home / "lab-run/runs" / f"{run_id}.ctx/gh-calls.log"
    if lane_log.exists():
        result["lane"] = "\n".join(bounded_lines(lane_log)) + "\n"
    if include_host:
        rows = []
        for day in (now - timedelta(days=1), now):
            for line in bounded_lines(
                cache_home / "omni/gh-usage" / f"{day:%Y-%m-%d}.jsonl"
            ):
                try:
                    row = json.loads(line)
                    if not isinstance(row, dict):
                        continue
                    at = datetime.strptime(row["ts"], "%Y-%m-%dT%H:%M:%SZ").replace(
                        tzinfo=UTC
                    )
                    if not now - timedelta(minutes=60) <= at <= now:
                        continue
                    if (
                        row.get("cache") in {"hit", "watcher"}
                        or row.get("method") == "LOCAL"
                        or row.get("endpoint") == "rate_limit"
                    ):
                        continue
                    fields = (
                        row["ts"],
                        row.get("lane") or "unattributed",
                        row.get("lane_source", "host"),
                        row.get("cls", ""),
                        row.get("method", ""),
                        row.get("endpoint", ""),
                        str(row.get("exit", -1)),
                        "allowed",
                    )
                    rows.append("\t".join(clean(str(value)) for value in fields))
                except (ValueError, KeyError, TypeError):
                    continue
        result["host"] = "\n".join(rows) + ("\n" if rows else "")
    return result


def collect_calls(
    ssh: Callable[..., Any],
    run_id: str,
    log_path: Path,
    *,
    host: str | None = None,
) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", run_id):
        raise ValueError("invalid run id")
    if host is not None and not re.fullmatch(r"[A-Za-z0-9_.-]+", host):
        raise ValueError("invalid host")
    script = (
        "python3 "
        f"-c {shlex.quote('from omnimarket.nodes.node_lane_github_budget_effect import main; raise SystemExit(main())')} --host-calls {shlex.quote(run_id)} {int(host is not None)}"
    )
    done = ssh(script, timeout=30)
    if done.returncode:
        raise OSError(f"GitHub accounting relay rc={done.returncode}")
    snapshots = json.loads(done.stdout)
    counts = {}
    for source, name in (("lane", f"run-{run_id}"), ("host", f"host-{host}")):
        if source not in snapshots or (source == "host" and host is None):
            continue
        text = snapshots[source]
        if not isinstance(text, str):
            raise TypeError("invalid GitHub accounting snapshot")
        target = log_path.parent / "gh-calls.d" / f"{name}.log"
        target.parent.mkdir(parents=True, exist_ok=True)
        # Concurrent lanes relay the same host file by atomic replacement, never append.
        with tempfile.NamedTemporaryFile(
            mode="w", dir=target.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(text)
        temporary.replace(target)
        counts[source] = len(text.splitlines())
    return counts


@contextmanager
def refresh_scope(
    remote_path: str | None,
    refresh: Callable[[], None],
    collect: Callable[[], Any] | None,
    interval: float,
    info: dict[str, Any],
    echo: Callable[[str], None],
) -> Iterator[dict[str, Any]]:
    stop = threading.Event()

    def tick() -> None:
        if remote_path:
            refresh()
        if collect:
            try:
                info["github_accounting"] = collect()
                info.pop("github_accounting_error", None)
            except (OSError, ValueError, TypeError, subprocess.SubprocessError) as exc:
                info["github_accounting_error"] = str(exc)[:200]
                echo(f"REMOTE-LANE-GITHUB-ACCOUNTING-FAILED {exc}")

    def loop() -> None:
        while not stop.wait(interval):
            tick()

    if not remote_path and not collect:
        info["skipped"] = True
        yield info
        return
    tick()
    worker = threading.Thread(target=loop, name="lane-state-refresh", daemon=True)
    worker.start()
    try:
        yield info
    finally:
        stop.set()
        worker.join()
        if collect:
            tick()
        info["stopped"] = True
