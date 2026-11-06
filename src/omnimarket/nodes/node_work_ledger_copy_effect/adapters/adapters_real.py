# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The real seams: ``pg_dump`` and ``psql`` for the source, ``ssh`` and ``rsync`` for the target store,
a throwaway postgres container on the target host for the drill, and the ledger append for the report.

Every command is an argument vector run without a shell on this host; the only remote shell text is
built here from validated names. The database password is read from the environment (or the named
dotenv file) into the child process environment and never appears in an argument vector or a message.
"""

from __future__ import annotations

import hashlib
import re
import shlex
import subprocess
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

from omnimarket.nodes.node_work_ledger_copy_effect.models import (
    ModelCopyOverlay,
    ModelCopyResult,
    ModelFileFacts,
    ModelRowFacts,
)
from omnimarket.nodes.node_work_ledger_copy_effect.protocols import CopyStepError

Runner = Callable[
    [Sequence[str], Mapping[str, str] | None, int], "subprocess.CompletedProcess[str]"
]
_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")
_RUN_ID = re.compile(r"^[0-9]{8}T[0-9]{6}Z$")
_SSH = ("ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10")


def run_command(
    argv: Sequence[str], env: Mapping[str, str] | None, timeout: int
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            check=False,
            env=dict(env) if env is not None else None,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        return subprocess.CompletedProcess(list(argv), 127, "", str(exc))
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            list(argv), 124, "", f"timed out after {timeout}s"
        )


def expand_home(value: str) -> str:
    """``~/`` at the start of a value, or after a ``:`` or ``=``, names the running user's home.

    An overlay kept in a repository names no home directory; the host resolves it when it runs.
    """
    home = str(Path.home())
    if value == "~":
        return home
    return re.sub(r"(^|[:=])~/", lambda m: f"{m.group(1)}{home}/", value)


def _first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()[:300]
    return "no output"


def read_dotenv_key(path: Path, key: str) -> str | None:
    """One ``KEY=value`` line of a dotenv file, read without exporting anything."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        name, sep, value = line.partition("=")
        if sep and name.strip() == key:
            return value.strip().strip("'\"")
    return None


def file_facts(data: bytes) -> ModelFileFacts:
    lines = data.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    last = lines[-1] if lines else b""
    return ModelFileFacts(
        size_bytes=len(data),
        line_count=len(lines),
        last_line_sha256=hashlib.sha256(last).hexdigest(),
    )


class PostgresSource:
    def __init__(
        self,
        overlay: ModelCopyOverlay,
        environ: Mapping[str, str],
        run: Runner = run_command,
    ) -> None:
        source = overlay.source
        for name in (source.schema_name, source.rows_table, source.state_table):
            if not _IDENT.match(name):
                raise ValueError(f"{name!r} is not a plain identifier")
        if not re.match(r"^[A-Za-z0-9_.-]+$", source.ledger_id):
            raise ValueError("ledger_id holds a character a query literal cannot carry")
        self._o = overlay
        self._environ = environ
        self._run = run

    def _child_env(self) -> dict[str, str]:
        source = self._o.source
        password = self._environ.get(source.password_env, "")
        if not password and source.env_file:
            password = (
                read_dotenv_key(Path(source.env_file).expanduser(), source.password_env)
                or ""
            )
        if not password:
            raise CopyStepError(
                "dump",
                f"{source.password_env} is in neither the environment nor {source.env_file or 'a named env file'}",
            )
        return {
            "PGPASSWORD": password,
            "PATH": self._environ.get("PATH", "/usr/bin:/bin"),
        }

    def _connection(self) -> list[str]:
        s = self._o.source
        return ["-h", s.host, "-p", str(s.port), "-U", s.user, "-d", s.database]

    def _psql(self, sql: str) -> str:
        s = self._o.source
        done = self._run(
            [
                f"{expand_home(s.pg_bin_dir)}/psql",
                *self._connection(),
                "-X",
                "-At",
                "-v",
                "ON_ERROR_STOP=1",
                "-c",
                sql,
            ],
            self._child_env(),
            self._o.timeout_seconds,
        )
        if done.returncode:
            raise CopyStepError(
                "dump", f"psql exit {done.returncode}: {_first_line(done.stderr)}"
            )
        return done.stdout.strip()

    def cutoff(self) -> datetime:
        text = self._psql(
            "select to_char(clock_timestamp() at time zone 'utc','YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"')"
        )
        try:
            return datetime.strptime(text, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
        except ValueError as exc:
            raise CopyStepError("dump", f"the database clock read {text!r}") from exc

    def row_facts(self, cutoff: datetime) -> ModelRowFacts:
        s = self._o.source
        stamp = cutoff.strftime("%Y-%m-%dT%H:%M:%S.%f+00")
        rows = f"{s.schema_name}.{s.rows_table}"
        window = f"ledger_id = '{s.ledger_id}' and projected_at <= '{stamp}'"
        out = self._psql(
            f"select count(*) || '|' || coalesce((select row_id from {rows} where {window} "
            f"order by projected_at desc, row_id desc limit 1), '') || '|' || "
            f"(select count(*) from {s.schema_name}.{s.state_table}) from {rows} where {window}"
        )
        count, last, state = out.split("|")
        return ModelRowFacts(
            row_count=int(count), last_row_id=last, state_count=int(state)
        )

    def dump(self, workdir: Path) -> Path:
        s = self._o.source
        target = workdir / "projection.dump"
        done = self._run(
            [
                f"{expand_home(s.pg_bin_dir)}/pg_dump",
                *self._connection(),
                "-Fc",
                "-Z",
                "6",
                "-t",
                f"{s.schema_name}.{s.rows_table}",
                "-t",
                f"{s.schema_name}.{s.state_table}",
                "-f",
                str(target),
            ],
            self._child_env(),
            self._o.timeout_seconds,
        )
        if done.returncode or not target.is_file():
            raise CopyStepError(
                "dump", f"pg_dump exit {done.returncode}: {_first_line(done.stderr)}"
            )
        return target

    def snapshot_file(self, workdir: Path) -> tuple[Path, ModelFileFacts]:
        raw = self._environ.get(self._o.file.path_env, "").strip()
        if not raw:
            raise CopyStepError("dump", f"{self._o.file.path_env} is not set")
        try:
            data = Path(raw).expanduser().resolve().read_bytes()
        except OSError as exc:
            raise CopyStepError(
                "dump", f"the ledger file cannot be read: {exc}"
            ) from exc
        end = (
            data.rfind(b"\n") + 1
        )  # a row still being appended is not part of the copy
        data = data[:end]
        target = workdir / "ledger.md"
        target.write_bytes(data)
        return target, file_facts(data)

    def archive_dir(self) -> Path:
        raw = self._environ.get(self._o.file.path_env, "").strip()
        return Path(raw).expanduser().resolve().parent / self._o.file.archive_dirname


class SshStore:
    def __init__(self, overlay: ModelCopyOverlay, run: Runner = run_command) -> None:
        self._t = overlay.target
        self._timeout = overlay.timeout_seconds
        self._run = run

    def _ssh(self, command: str) -> subprocess.CompletedProcess[str]:
        return self._run([*_SSH, self._t.ssh_target, command], None, self._timeout)

    def _must(self, command: str, step: str = "transfer") -> str:
        done = self._ssh(command)
        if done.returncode:
            raise CopyStepError(
                step, f"remote exit {done.returncode}: {_first_line(done.stderr)}"
            )
        return done.stdout

    def free_bytes(self) -> int:
        directory = shlex.quote(self._t.directory)
        out = self._must(
            f"mkdir -p {directory} && df -B1 --output=avail {directory} | tail -n 1"
        )
        try:
            return int(out.strip())
        except ValueError as exc:
            raise CopyStepError("transfer", f"free space read {out.strip()!r}") from exc

    def put(self, run_id: str, files: Sequence[Path], archive_dir: Path) -> int:
        base = shlex.quote(self._t.directory)
        run_dir = f"{self._t.directory}/{run_id}"
        self._must(f"mkdir -p {shlex.quote(run_dir)} {base}/archive")
        rsh = " ".join(_SSH)
        for argv in (
            [
                "rsync",
                "-a",
                "-e",
                rsh,
                *map(str, files),
                f"{self._t.ssh_target}:{run_dir}/",
            ],
            [
                "rsync",
                "-a",
                "-e",
                rsh,
                f"{archive_dir}/",
                f"{self._t.ssh_target}:{self._t.directory}/archive/",
            ],
        ):
            done = self._run(argv, None, self._timeout)
            if done.returncode:
                raise CopyStepError(
                    "transfer",
                    f"rsync exit {done.returncode}: {_first_line(done.stderr)}",
                )
        out = self._must(f"du -sb {base} | cut -f1")
        return int(out.strip() or 0)

    def digests(self, run_id: str) -> dict[str, str]:
        run_dir = shlex.quote(f"{self._t.directory}/{run_id}")
        out = self._must(f"cd {run_dir} && sha256sum projection.dump ledger.md")
        found: dict[str, str] = {}
        for line in out.splitlines():
            digest, _, name = line.partition("  ")
            found[name.strip()] = digest
        return found

    def prune(self, keep: int) -> tuple[str, ...]:
        out = self._must(f"ls -1 {shlex.quote(self._t.directory)}")
        runs = sorted(name for name in out.split() if _RUN_ID.match(name))
        doomed = runs[:-keep] if len(runs) > keep else []
        for name in doomed:
            self._must(f"rm -rf {shlex.quote(self._t.directory + '/' + name)}")
        return tuple(doomed)


class DockerDrill:
    def __init__(self, overlay: ModelCopyOverlay, run: Runner = run_command) -> None:
        self._o = overlay
        self._run = run

    def _ssh(self, command: str) -> subprocess.CompletedProcess[str]:
        return self._run(
            [*_SSH, self._o.target.ssh_target, command],
            None,
            self._o.target.drill.timeout_seconds,
        )

    def _must(self, command: str) -> str:
        done = self._ssh(command)
        if done.returncode:
            raise CopyStepError(
                "drill", f"remote exit {done.returncode}: {_first_line(done.stderr)}"
            )
        return done.stdout

    def restore(
        self, run_id: str, cutoff: datetime
    ) -> tuple[ModelRowFacts, ModelFileFacts]:
        t, d, s = self._o.target, self._o.target.drill, self._o.source
        name = f"ledger-copy-drill-{run_id}"
        run_dir = f"{t.directory}/{run_id}"
        stamp = cutoff.strftime("%Y-%m-%dT%H:%M:%S.%f+00")
        db = d.scratch_database
        if not (_IDENT.match(db) and _RUN_ID.match(run_id)):
            raise CopyStepError(
                "drill", "scratch database or run id is not a plain name"
            )
        rows = f"{s.schema_name}.{s.rows_table}"
        window = f"ledger_id = '{s.ledger_id}' and projected_at <= '{stamp}'"
        query = (
            f"select count(*) || '|' || coalesce((select row_id from {rows} where {window} "
            f"order by projected_at desc, row_id desc limit 1), '') from {rows} where {window}"
        )
        exec_psql = (
            f"docker exec {name} psql -U postgres -d {db} -X -At -v ON_ERROR_STOP=1"
        )
        start = (
            f"docker run -d --rm --name {name} --memory {shlex.quote(d.memory_limit)} "
            f"--tmpfs /var/lib/postgresql/data -e POSTGRES_HOST_AUTH_METHOD=trust "
            f"-e POSTGRES_DB={db} -v {shlex.quote(run_dir)}:/in:ro {shlex.quote(d.image)}"
        )
        wait = (
            f"for i in $(seq 1 {d.timeout_seconds}); do "
            f"[ \"$(docker logs {name} 2>&1 | grep -c 'ready to accept connections')\" -ge 2 ] && exit 0; "
            f"sleep 1; done; exit 1"
        )
        try:
            self._must(start)
            self._must(wait)
            self._must(
                f"{exec_psql} -c {shlex.quote(f'create schema {s.schema_name}')}"
            )
            self._must(
                f"docker exec {name} pg_restore -U postgres -d {db} --no-owner --no-privileges "
                f"--exit-on-error /in/projection.dump"
            )
            out = self._must(f"{exec_psql} -c {shlex.quote(query)}").strip()
            state = self._must(
                f"{exec_psql} -c {shlex.quote(f'select count(*) from {s.schema_name}.{s.state_table}')}"
            ).strip()
            count, _, last = out.partition("|")
            facts = ModelRowFacts(
                row_count=int(count), last_row_id=last, state_count=int(state)
            )
            file_out = self._must(
                f'cd {shlex.quote(run_dir)} && echo "$(wc -c < ledger.md) $(wc -l < ledger.md) '
                f"$(tail -n 1 ledger.md | tr -d '\\n' | sha256sum | cut -d' ' -f1)\""
            ).split()
            restored_file = ModelFileFacts(
                size_bytes=int(file_out[0]),
                line_count=int(file_out[1]),
                last_line_sha256=file_out[2],
            )
        except (ValueError, IndexError) as exc:
            raise CopyStepError(
                "drill", f"a drill read could not be parsed: {exc}"
            ) from exc
        finally:
            self._ssh(f"docker rm -f {name} >/dev/null 2>&1; true")
        return facts, restored_file


class AppendReporter:
    """Appends one STATUS row per run through the configured ledger append command."""

    def __init__(
        self,
        overlay: ModelCopyOverlay,
        run: Runner = run_command,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._r = overlay.report
        self._timeout = overlay.timeout_seconds
        self._run = run
        self._environ = environ

    def row(self, result: ModelCopyResult, now: datetime) -> str:
        rows = result.restored_rows or result.source_rows
        lines = result.restored_file or result.source_file
        detail = re.sub(r"\s*\|\s*|\s+", " ", result.detail).strip()[:240]
        return " | ".join(
            [
                now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "STATUS",
                f"lane={self._r.lane}",
                "src=ledger-copy",
                f"actor={self._r.actor}",
                "model=none",
                f"run={result.run_id}",
                f"outcome={result.outcome.value}",
                f"rows={rows.row_count if rows else 'none'}",
                f"file_lines={lines.line_count if lines else 'none'}",
                f"copy_bytes={result.copy_bytes}",
                detail,
            ]
        )

    def report(self, result: ModelCopyResult) -> None:
        done = self._run(
            [
                *map(expand_home, self._r.append_argv),
                self.row(result, datetime.now(UTC)),
            ],
            self._environ,
            self._timeout,
        )
        if done.returncode:
            raise CopyStepError(
                "report",
                f"the ledger append exited {done.returncode}: {_first_line(done.stderr)}",
            )
