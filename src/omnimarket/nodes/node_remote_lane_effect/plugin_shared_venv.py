# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One venv per lock hash for the lab plugin store. Standalone stdlib payload for a remote Python interpreter.

A plugin tree under the store's plugins directory used to carry its own ``.venv`` (about 49,000 files for the omniclaude
tree), created by ``uv sync`` for every plugin commit a lane placement touched. This module builds that environment once
per ``uv.lock`` hash, in ``<store>/<venvs_dir>/<key>``, and gives each tree a small ``.venv`` of its own that reaches it:

- ``pyvenv.cfg`` and the interpreter links, so ``<tree>/.venv/bin/python3`` is a venv of the tree;
- one ``.pth`` that adds the shared site-packages (and runs the ``.pth`` files in it);
- the tree's own project, installed editable into the tree's small ``.venv`` (the shared one is built without the project,
  because the project differs per commit while the locked dependencies do not).

A shared venv that is ready is never changed: a new lock gets a new venv, and one no tree links to is pruned by the same
retention that prunes trees. Every value (lock file name, directory names, uv, time limits) arrives from the caller; the
host runs this text with its own ``python3`` and imports nothing installed.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import time
from hashlib import sha256
from pathlib import Path
from typing import Any

READY = ".ready"
MARKER = ".shared-venv"
LINKING = ".linking"
LOCK_SUFFIX = ".building"
PRUNING = ".pruning."
_KEY_LENGTH = 16
_LOCK_GRACE_S = 10.0


class SharedVenvError(Exception):
    """A refusal with a typed code; the caller prints it and the step fails closed."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


def lock_key(lock: Path) -> str:
    """The first sixteen hex digits of the lock file's SHA-256: two trees with the same lock share one venv."""
    try:
        return sha256(lock.read_bytes()).hexdigest()[:_KEY_LENGTH]
    except OSError as exc:
        raise SharedVenvError("no-lock", f"{lock}: {exc.strerror or exc}") from exc


def _remove(path: Path) -> None:
    def writable(func: Any, target: str, _exc: Any) -> None:
        os.chmod(os.path.dirname(target), 0o700)
        if not os.path.islink(target):
            os.chmod(target, 0o700)
        func(target)

    if os.path.islink(path) or not os.path.isdir(path):
        os.unlink(path)
    else:
        shutil.rmtree(path, onerror=writable)


def _env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("UV_PROJECT_ENVIRONMENT", "VIRTUAL_ENV")
    }
    env.update(extra or {})
    return env


def _stale(lock: Path, limit: float) -> bool:
    age = time.time() - lock.stat().st_mtime
    try:
        pid = int((lock / "pid").read_text().strip())
    except (OSError, ValueError):
        return age > _LOCK_GRACE_S
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        pass
    return age > limit * 2


def _acquire(lock: Path, ready: Any, timeout: float) -> bool:
    """True when this process now holds ``lock``; False when ``ready()`` turned true while another process held it."""
    deadline = time.time() + timeout
    while True:
        try:
            lock.mkdir()
            (lock / "pid").write_text(str(os.getpid()))
            return True
        except FileExistsError:
            pass
        if ready():
            return False
        try:
            if _stale(lock, timeout):
                _remove(lock)
                continue
        except OSError:
            continue
        if time.time() > deadline:
            raise SharedVenvError("lock-timeout", str(lock))
        time.sleep(0.5)


def _release(lock: Path) -> None:
    with contextlib.suppress(OSError):
        _remove(lock)


def _uv(
    argv: list[str], *, cwd: Path, env: dict[str, str], timeout: float, run: Any
) -> None:
    try:
        done = run(
            argv, cwd=str(cwd), env=env, capture_output=True, text=True, timeout=timeout
        )
    except FileNotFoundError as exc:
        raise SharedVenvError("uv-missing", argv[0]) from exc
    except subprocess.TimeoutExpired as exc:
        raise SharedVenvError("uv-timeout", " ".join(argv[1:4])) from exc
    if done.returncode != 0:
        tail = (done.stderr or done.stdout or "").strip()[-300:]
        raise SharedVenvError(
            "uv-failed", f"{' '.join(argv[1:4])} rc={done.returncode}: {tail}"
        )


def _site_packages(venv: Path) -> Path:
    """``<venv>/lib/pythonX.Y/site-packages``, with X.Y read from the venv's own ``pyvenv.cfg``."""
    try:
        text = (venv / "pyvenv.cfg").read_text()
    except OSError as exc:
        raise SharedVenvError("shared-venv-unreadable", f"{venv}/pyvenv.cfg") from exc
    for line in text.splitlines():
        name, _, value = line.partition("=")
        if name.strip() in ("version_info", "version"):
            major_minor = ".".join(value.strip().split(".")[:2])
            return venv / "lib" / f"python{major_minor}" / "site-packages"
    raise SharedVenvError("shared-venv-unreadable", "no version_info in pyvenv.cfg")


def ensure_shared(
    tree: Path,
    store: Path,
    *,
    lock_file: str,
    venvs_dir: str,
    uv: str,
    build_timeout_s: float,
    run: Any = subprocess.run,
) -> tuple[Path, str, bool]:
    """The shared venv for ``tree``'s lock: ``(its path, its key, built by this call)``. A ready one is only touched."""
    key = lock_key(tree / lock_file)
    shared = store / venvs_dir / key
    ready = shared / READY

    def touch() -> None:
        with contextlib.suppress(OSError):
            os.utime(shared, None)

    if ready.is_file():
        touch()
        return shared, key, False
    (store / venvs_dir).mkdir(parents=True, exist_ok=True)
    lock = shared.with_name(key + LOCK_SUFFIX)
    if not _acquire(lock, ready.is_file, build_timeout_s):
        touch()
        return shared, key, False
    try:
        if ready.is_file():
            touch()
            return shared, key, False
        if shared.exists():
            _remove(
                shared
            )  # a crashed build: nothing links to a venv before its ready marker
        try:
            _uv(
                [uv, "sync", "--frozen", "--no-install-project", "--quiet"],
                cwd=tree,
                env=_env({"UV_PROJECT_ENVIRONMENT": str(shared)}),
                timeout=build_timeout_s,
                run=run,
            )
            if not (shared / "bin" / "python3").exists():
                raise SharedVenvError("shared-venv-no-interpreter", str(shared))
            _site_packages(shared)
        except BaseException:
            if shared.exists():
                _remove(shared)
            raise
        ready.write_text(json.dumps({"key": key, "lock": lock_file}) + "\n")
        return shared, key, True
    finally:
        _release(lock)


def _link_interpreters(shared: Path, own: Path) -> None:
    (own / "bin").mkdir()
    for name in sorted(os.listdir(shared / "bin")):
        if not name.startswith("python"):
            continue
        source = shared / "bin" / name
        target = os.readlink(source) if source.is_symlink() else str(source)
        # a sibling name ("python") stays relative so the link resolves inside the tree's own bin
        if not os.path.isabs(target) and os.sep in target:
            target = os.path.normpath(os.path.join(shared / "bin", target))
        os.symlink(target, own / "bin" / name)


def count_files(root: Path) -> int:
    return sum(len(files) for _, _, files in os.walk(root))


def _readback(own: Path, shared_site: Path) -> None:
    done = subprocess.run(
        [
            str(own / "bin" / "python3"),
            "-c",
            "import sys; sys.exit(0 if sys.argv[1] in sys.path else 1)",
            str(shared_site),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        env=_env(),
    )
    if done.returncode != 0:
        raise SharedVenvError(
            "link-readback-failed", f"{own}: {(done.stderr or '').strip()[-200:]}"
        )


def link_tree(
    tree: Path,
    shared: Path,
    key: str,
    *,
    uv: str,
    build_timeout_s: float,
    run: Any = subprocess.run,
) -> bool:
    """Give ``tree`` its small ``.venv``; True when this call made it. A ``.venv`` this module did not make is left alone."""
    own = tree / ".venv"
    marker = own / MARKER
    lock = tree / (".venv" + LOCK_SUFFIX)

    def settled() -> bool:
        return marker.is_file() and (own / "bin" / "python3").exists()

    def foreign() -> bool:
        return own.exists() and not marker.is_file() and not (own / LINKING).exists()

    if settled() or foreign():
        return False
    if not _acquire(lock, lambda: settled() or foreign(), build_timeout_s):
        return False
    try:
        if settled() or foreign():
            return False
        if own.exists():
            _remove(
                own
            )  # a crashed link of this module, or one whose interpreter went (its shared venv was pruned)
        shared_site = _site_packages(shared)
        try:
            own.mkdir()
            (own / LINKING).write_text(key + "\n")
            (own / "pyvenv.cfg").write_text((shared / "pyvenv.cfg").read_text())
            _link_interpreters(shared, own)
            site = own / shared_site.relative_to(shared)
            site.mkdir(parents=True)
            (site / "_shared_plugin_venv.pth").write_text(
                f"import site; site.addsitedir({str(shared_site)!r})\n"
            )
            _readback(own, shared_site)
            _uv(
                [
                    uv,
                    "pip",
                    "install",
                    "--python",
                    str(own / "bin" / "python3"),
                    "--no-deps",
                    "--editable",
                    str(tree),
                    "--quiet",
                ],
                cwd=tree,
                env=_env(),
                timeout=build_timeout_s,
                run=run,
            )
            marker.write_text(json.dumps({"key": key, "shared": str(shared)}) + "\n")
            (own / LINKING).unlink()
        except BaseException:
            if own.exists():
                _remove(own)
            raise
        return True
    finally:
        _release(lock)


def ensure_tree_venv(
    tree: Path,
    store: Path,
    *,
    lock_file: str = "uv.lock",
    venvs_dir: str = "venvs",
    uv: str = "uv",
    build_timeout_s: float = 900.0,
    run: Any = subprocess.run,
) -> dict[str, Any]:
    """Build (once) the shared venv for the tree's lock and link the tree to it."""
    shared, key, built = ensure_shared(
        tree,
        store,
        lock_file=lock_file,
        venvs_dir=venvs_dir,
        uv=uv,
        build_timeout_s=build_timeout_s,
        run=run,
    )
    linked = link_tree(
        tree, shared, key, uv=uv, build_timeout_s=build_timeout_s, run=run
    )
    if not (tree / ".venv" / "bin" / "python3").exists():
        raise SharedVenvError("no-interpreter", str(tree / ".venv"))
    return {
        "key": key,
        "shared": str(shared),
        "built": built,
        "linked": linked,
        "tree_venv_files": count_files(tree / ".venv"),
    }


def _linked_keys(plugins: Path) -> set[str]:
    keys: set[str] = set()
    for tree in plugins.iterdir() if plugins.is_dir() else ():
        try:
            keys.add(json.loads((tree / ".venv" / MARKER).read_text())["key"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return keys


def prune_shared(
    store: Path,
    *,
    plugins_dir: str,
    venvs_dir: str,
    retain_days: float,
    cap: int,
    build_timeout_s: float,
) -> dict[str, int]:
    """Remove shared venvs no tree links to, untouched for ``retain_days``; at most ``cap`` per call.

    A linked venv is kept whatever its age. A venv a lane touched since the read (``ensure_shared`` touches it first) is put
    back, and a lane whose link lost that race fails its readback and says so."""
    root = store / venvs_dir
    counts = {"pruned": 0, "linked": 0, "recent": 0, "deferred": 0, "leftovers": 0}
    if not root.is_dir():
        return counts
    now = time.time()
    linked = _linked_keys(store / plugins_dir)
    for name in sorted(os.listdir(root)):
        path = root / name
        if name.startswith(PRUNING):
            _remove(path)
            counts["leftovers"] += 1
        elif name.endswith(LOCK_SUFFIX):
            if _stale(path, build_timeout_s):
                _remove(path)
                counts["leftovers"] += 1
        elif path.is_dir() and not path.is_symlink():
            if name in linked:
                counts["linked"] += 1
            elif now - path.stat().st_mtime < retain_days * 86400:
                counts["recent"] += 1
            elif counts["pruned"] >= cap:
                counts["deferred"] += 1
            else:
                moved = root / f"{PRUNING}{name}.{os.getpid()}"
                os.rename(path, moved)
                if (
                    name in _linked_keys(store / plugins_dir)
                    or time.time() - moved.stat().st_mtime < retain_days * 86400
                ):
                    os.rename(moved, path)
                    counts["recent"] += 1
                    continue
                _remove(moved)
                counts["pruned"] += 1
                sys.stderr.write("PLUGIN-VENV-PRUNED " + name + "\n")
    return counts


def cli(argv: list[str], run: Any = subprocess.run) -> int:
    """``ensure <config> <tree> <store>`` or ``prune <config> <store> <retain-days> <cap>``; the lines are what the step reads."""
    try:
        mode, config = argv[0], json.loads(argv[1])
        if mode == "ensure":
            done = ensure_tree_venv(
                Path(argv[2]),
                Path(argv[3]),
                lock_file=config["lock_file"],
                venvs_dir=config["venvs_dir"],
                uv=config["uv"],
                build_timeout_s=float(config["build_timeout_s"]),
                run=run,
            )
            sys.stdout.write(
                "PLUGIN-VENV-READY " + json.dumps(done, sort_keys=True) + "\n"
            )
            return 0
        if mode == "prune":
            counts = prune_shared(
                Path(argv[2]),
                plugins_dir=config["plugins_dir"],
                venvs_dir=config["venvs_dir"],
                retain_days=float(argv[3]),
                cap=int(argv[4]),
                build_timeout_s=float(config["build_timeout_s"]),
            )
            sys.stderr.write(
                "PLUGIN-VENV-PRUNE-SUMMARY "
                + " ".join(f"{k}={v}" for k, v in sorted(counts.items()))
                + "\n"
            )
            return 0
        raise SharedVenvError("bad-mode", mode)
    except SharedVenvError as exc:
        sys.stdout.write(f"PLUGIN-VENV-REFUSED {exc.code} {exc.detail}".rstrip() + "\n")
        return 21
    except (OSError, ValueError, KeyError, IndexError) as exc:
        sys.stdout.write(f"PLUGIN-VENV-REFUSED {type(exc).__name__} {exc}\n")
        return 21


def _payload() -> str:
    """This module's text up to the generators: what the host's ``python3`` runs."""
    return Path(__file__).read_text().split("\ndef shared_venv_script(", 1)[0]


def shared_venv_script(
    tree_var: str,
    store_var: str,
    *,
    lock_file: str,
    venvs_dir: str,
    uv: str,
    build_timeout_s: int,
    suffix: str = "",
) -> str:
    """Remote shell for one tree: build the tree's shared venv once and link the tree to it.

    ``tree_var`` and ``store_var`` are shell variables of the caller's script (for example ``D`` and ``LR``). The script
    prints ``PLUGIN-VENV-READY {json}`` and exits 0, or prints ``PLUGIN-VENV-REFUSED <code> <detail>`` and exits 21.
    ``suffix`` is shell text for the end of the command line (for example ``|| exit 21``); a here-document body starts on the
    line after it, so the caller cannot add it after the script."""
    import shlex

    config = json.dumps(
        {
            "lock_file": lock_file,
            "venvs_dir": venvs_dir,
            "uv": uv,
            "build_timeout_s": build_timeout_s,
        },
        sort_keys=True,
    )
    return (
        f'python3 - ensure {shlex.quote(config)} "${tree_var}" "${store_var}" <<\'PLUGIN_SHARED_VENV_PY\'{suffix}\n'
        + _payload()
        + "\nraise SystemExit(cli(sys.argv[1:]))\nPLUGIN_SHARED_VENV_PY"
    )


def shared_venv_prune_script(
    store_var: str,
    *,
    plugins_dir: str,
    venvs_dir: str,
    retain_days_word: str,
    cap_word: str,
    build_timeout_s: int,
    suffix: str = "",
) -> str:
    """Remote shell: prune the shared venvs no tree links to. ``retain_days_word`` and ``cap_word`` are shell words (for
    example ``"${ONEX_PLUGIN_RETAIN_DAYS:-1}"``) so the host's environment can set them. The report goes to stderr and the
    exit status is the prune's; ``suffix`` is shell text for the end of the command line (for example ``>&2 || true``)."""
    import shlex

    config = json.dumps(
        {
            "plugins_dir": plugins_dir,
            "venvs_dir": venvs_dir,
            "build_timeout_s": build_timeout_s,
        },
        sort_keys=True,
    )
    return (
        f"python3 - prune {shlex.quote(config)} \"${store_var}\" {retain_days_word} {cap_word} <<'PLUGIN_SHARED_VENV_PY'{suffix}\n"
        + _payload()
        + "\nraise SystemExit(cli(sys.argv[1:]))\nPLUGIN_SHARED_VENV_PY"
    )
