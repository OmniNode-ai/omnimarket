# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Fill each lab host's Claude Code token pool from the one pool in the secret store (OMN-21626).

Operator RULING 2026-10-31T01:21:16Z: one pool of lab Claude Code tokens, kept in the secret store and
available to every machine. Each lab host keeps a local copy of that pool (node_claude_token_pool_effect)
and its lane runner exports the first token there that is not marked limited or revoked. For a named
host or every host the private overlay declares, one run:

1. reads the host's local pool (labels, fingerprints, revoked and limited marks, never a value);
2. adds every store token the host is missing, or holds under an older value, through a pipe from the
   reader host's store read (the lane secret resolver) straight into the host's pool ``add``, in the
   order that host draws them: the overlay's token order rotated by the host's place in the overlay's
   host list, so no two hosts draw the same first token, then sets that order on the host's pool;
3. probes the credential the host's lane runner would draw (the pool's ``resolve``) with one Claude Code
   call placed by ``node_llm_delegation_call_effect`` (the typed credential probe); an expired, revoked
   or refused answer marks that label revoked and the next label is probed, until one answers or none
   is left. A usage limit belongs to the account, not the host: the label is marked limited on every
   served host whose pool holds the same token, so no other host spends a probe finding it again;
4. returns one summary line per host for the caller's ledger row.

Every host, path, key name, the model and the answer patterns come from the overlay at
``$ONEX_WORKSPACE_CONFIG_ROOT/config/onex/overlays/node_claude_pool_refill_effect/overlay.yaml``. A token value only travels through a
pipe between two remote sessions, or sits in a shell variable on the host whose pool it is; this process
never reads one, and every excerpt it returns is redacted. The command line is ``runtime_claude_pool_refill``.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from omnimarket.models.delegation.model_claude_credential_probe import (
    ModelClaudeCredentialProbeReceipt,
    ModelClaudeCredentialProbeRequest,
)
from omnimarket.nodes.node_claude_pool_refill_effect.models.model_claude_pool_refill import (
    ModelClaudePoolRefillRequest,
    ModelClaudePoolRefillResult,
)

OVERLAY_REL = Path("config/onex/overlays/node_claude_pool_refill_effect/overlay.yaml")
POOL_MODULE = "omnimarket.nodes.node_claude_token_pool_effect"
PROBE_MODULE = "omnimarket.nodes.node_llm_delegation_call_effect.runtime_claude_probe"
SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=8"]
_SAFE_PATH = re.compile(r"^[A-Za-z0-9_./${}~-]+$")
_SAFE_WORD = re.compile(r"^[A-Za-z0-9_.@:/-]+$")
_LABEL = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_KEY = re.compile(r"^[A-Z][A-Z0-9_]*$")
_REDACT = re.compile(r"sk-ant-[A-Za-z0-9_-]+")


class ClaudePoolRefillRefusedError(Exception):
    """A refusal; its text names files, hosts and labels, never a value."""


@dataclass(frozen=True)
class Token:
    label: str
    key: str


@dataclass(frozen=True)
class Host:
    name: str
    target: str
    local: bool


@dataclass
class Overlay:
    path: Path
    folder: str
    project_id: str
    environment: str
    identity_env_file: str
    reader: Host
    tokens: list[Token]
    hosts: list[Host]
    venv_roots: list[str]
    path_prepend: list[str]
    model: str
    prompt: str
    timeout_s: int
    limited_seconds: int
    patterns: dict[str, list[str]] = field(default_factory=dict)


def _need(mapping: dict[str, Any], key: str, where: str) -> Any:
    if key not in mapping or mapping[key] in (None, "", []):
        raise ClaudePoolRefillRefusedError(f"{where} declares no {key!r}")
    return mapping[key]


def _safe(value: str, pattern: re.Pattern[str], what: str) -> str:
    if not pattern.match(value):
        raise ClaudePoolRefillRefusedError(
            f"{what} {value!r} holds characters this script will not put in a remote shell"
        )
    return value


def config_root(path: Path) -> Path:
    """The workspace config root ``hosts_table`` is relative to: the declared root, else the overlay's own."""
    declared = os.environ.get("ONEX_WORKSPACE_CONFIG_ROOT", "")
    return (
        Path(declared)
        if declared
        else path.resolve().parents[len(OVERLAY_REL.parts) - 1]
    )


def load_overlay(path: Path) -> Overlay:
    if not path.is_file():
        raise ClaudePoolRefillRefusedError(
            f"no pool overlay at {path}; set ONEX_WORKSPACE_CONFIG_ROOT to the private config root or pass --config"
        )
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ClaudePoolRefillRefusedError(f"{path} has no mapping at its root")
    where = str(path)
    store = _need(raw, "store", where)
    table_rel = _need(raw, "hosts_table", where)
    table_path = (
        (config_root(path) / table_rel)
        if not Path(table_rel).is_absolute()
        else Path(table_rel)
    )
    table_path = Path(os.environ.get("ONEX_LAB_RUN_HOSTS") or table_path)
    if not table_path.is_file():
        raise ClaudePoolRefillRefusedError(
            f"the hosts table {table_path} named by {where} does not exist"
        )
    rows = (yaml.safe_load(table_path.read_text(encoding="utf-8")) or {}).get(
        "hosts"
    ) or []
    by_name = {
        str(r["name"]): Host(
            str(r["name"]),
            _safe(str(r["target"]), _SAFE_WORD, "target"),
            bool(r.get("local")),
        )
        for r in rows
        if isinstance(r, dict) and r.get("name") and r.get("target")
    }

    def host(name: str) -> Host:
        if name not in by_name:
            raise ClaudePoolRefillRefusedError(
                f"host {name!r} named by {where} is not in {table_path}"
            )
        return by_name[name]

    tokens = []
    for row in _need(raw, "tokens", where):
        tokens.append(
            Token(
                _safe(str(row["label"]), _LABEL, "label"),
                _safe(str(row["key"]), _KEY, "key"),
            )
        )
    if len({t.label for t in tokens}) != len(tokens) or len(
        {t.key for t in tokens}
    ) != len(tokens):
        raise ClaudePoolRefillRefusedError(f"{where} repeats a token label or key")
    probe = _need(raw, "probe", where)
    patterns = _need(probe, "patterns", where + " probe")
    for verdict in ("refused", "expired", "limited"):
        _need(patterns, verdict, where + " probe.patterns")
    return Overlay(
        path=path,
        folder=_safe(
            str(_need(store, "folder", where + " store")), _SAFE_PATH, "store folder"
        ),
        project_id=_safe(
            str(_need(store, "project_id", where + " store")), _SAFE_WORD, "project id"
        ),
        environment=_safe(
            str(_need(store, "environment", where + " store")),
            _SAFE_WORD,
            "environment",
        ),
        identity_env_file=_safe(
            str(_need(store, "identity_env_file", where + " store")),
            _SAFE_PATH,
            "identity file",
        ),
        reader=host(str(_need(store, "reader_host", where + " store"))),
        tokens=tokens,
        hosts=[host(str(n)) for n in _need(raw, "hosts", where)],
        venv_roots=[
            _safe(str(r), _SAFE_PATH, "venv root")
            for r in _need(raw, "venv_roots", where)
        ],
        path_prepend=[
            _safe(str(p), _SAFE_PATH, "path entry")
            for p in raw.get("path_prepend") or []
        ],
        model=_safe(str(_need(probe, "model", where + " probe")), _SAFE_WORD, "model"),
        prompt=str(_need(probe, "prompt", where + " probe")),
        timeout_s=int(_need(probe, "timeout_s", where + " probe")),
        limited_seconds=int(_need(probe, "limited_seconds", where + " probe")),
        patterns={k: [str(p).lower() for p in v] for k, v in patterns.items()},
    )


# ---- remote shell -------------------------------------------------------------------------------


def _prelude(ov: Overlay) -> str:
    roots = " ".join(f'"{r}"' for r in ov.venv_roots)
    path = ":".join(ov.path_prepend)
    return (f'export PATH="{path}:$PATH"\n' if path else "") + (
        f'P=""; for r in {roots}; do if [ -x "$r/.onex-dispatch-venv/bin/python" ]; then '
        'P="$r/.onex-dispatch-venv/bin/python"; break; fi; done\n'
        '[ -n "$P" ] || { echo "NO-VENV"; exit 69; }\n'
    )


def _cmd(host: Host, script: str) -> list[str]:
    """The argv running ``script`` under bash on ``host``; its stdin stays free for a token pipe."""
    if host.local:
        return ["bash", "-c", script]
    return ["ssh", *SSH_OPTS, host.target, "bash -c " + shlex.quote(script)]


def _run(
    host: Host, script: str, *, timeout: float = 300
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        _cmd(host, script),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def host_pool(ov: Overlay, host: Host) -> dict[str, dict[str, Any]]:
    script = _prelude(ov) + f'cd /tmp; "$P" -m {POOL_MODULE} status --json\n'
    done = _run(host, script, timeout=60)
    if done.returncode != 0:
        raise ClaudePoolRefillRefusedError(
            f"{host.name}: pool status failed rc={done.returncode}: {_REDACT.sub('<redacted>', done.stdout + done.stderr)[-200:]}"
        )
    body = json.loads(done.stdout)
    return {t["label"]: t for t in body["tokens"]}


_READER_PY = r"""
import asyncio, hashlib, os, sys
env_file, project, environment, folder, mode = sys.argv[1:6]
keys = sys.argv[6:]
for line in open(os.path.expanduser(env_file), encoding="utf-8"):
    k, _, v = line.strip().removeprefix("export ").partition("=")
    if k in ("INFISICAL_ADDR", "INFISICAL_CLIENT_ID", "INFISICAL_CLIENT_SECRET"):
        os.environ[k] = v.strip().strip("'\"")
os.environ["INFISICAL_PROJECT_ID"] = project
os.environ["INFISICAL_ENVIRONMENT_SLUG"] = environment
from omnibase_infra.runtime.models.model_secret_resolver_config import ModelSecretResolverConfig
from omnimarket.inference.secret_store_resolver import secret_store_from_resolver_config
config = ModelSecretResolverConfig.model_validate({"enable_convention_fallback": False, "mappings": [
    {"logical_name": "claude.pool." + k.lower(), "source": {"source_type": "infisical", "source_path": folder.rstrip("/") + "/" + k}}
    for k in keys]})
store = secret_store_from_resolver_config(config)
for k in keys:
    v = asyncio.run(store.get_secret("claude.pool." + k.lower()))
    if mode == "fingerprints":
        print(k, hashlib.sha256(v.strip().encode()).hexdigest()[:8] if v else "none")
    elif v:
        if sys.stdout.isatty():
            sys.exit("refusing to write a token to a terminal")
        sys.stdout.write(v.strip() + "\n")
    else:
        sys.exit(3)
"""


def _reader_script(ov: Overlay, mode: str, keys: list[str]) -> str:
    args = " ".join(
        shlex.quote(a)
        for a in (
            ov.identity_env_file,
            ov.project_id,
            ov.environment,
            ov.folder,
            mode,
            *keys,
        )
    )
    return _prelude(ov) + f'cd /tmp; "$P" -I -c {shlex.quote(_READER_PY)} {args}\n'


def store_fingerprints(ov: Overlay) -> dict[str, str]:
    done = _run(
        ov.reader,
        _reader_script(ov, "fingerprints", [t.key for t in ov.tokens]),
        timeout=120,
    )
    if done.returncode != 0:
        raise ClaudePoolRefillRefusedError(
            f"store read on {ov.reader.name} failed rc={done.returncode}: {_REDACT.sub('<redacted>', done.stderr)[-300:]}"
        )
    out = dict(
        line.split() for line in done.stdout.splitlines() if line.count(" ") == 1
    )
    return {t.label: out.get(t.key, "none") for t in ov.tokens}


def install(ov: Overlay, host: Host, token: Token, *, force: bool) -> str:
    """Pipe one store value from the reader into the host's pool; returns the add line (label and fingerprint)."""
    reader_cmd = _reader_script(ov, "value", [token.key])
    add_cmd = _prelude(ov) + (
        f'cd /tmp; "$P" -m {POOL_MODULE} add --label {token.label}{" --force" if force else ""}'
    )
    reader = subprocess.Popen(
        _cmd(ov.reader, reader_cmd),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    target = subprocess.run(
        _cmd(host, add_cmd),
        stdin=reader.stdout,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert reader.stdout is not None
    reader.stdout.close()
    reader_err = (
        reader.stderr.read().decode("utf-8", "replace") if reader.stderr else ""
    )
    if reader.wait(timeout=120) != 0:
        return f"FAILED store-read {token.label}: {_REDACT.sub('<redacted>', reader_err)[-160:]}"
    text = (
        _REDACT.sub("<redacted>", target.stdout + target.stderr)
        .strip()
        .replace("\n", " ")
    )
    return (
        text if target.returncode == 0 else f"FAILED add {token.label}: {text[-200:]}"
    )


def probe(ov: Overlay, host: Host) -> tuple[str | None, str, str]:
    """(label the runner would draw, verdict, redacted excerpt), probing that label once.

    The call itself is placed by ``node_llm_delegation_call_effect`` on the host: the typed request goes in
    on its stdin, the token in its environment, and the typed receipt comes back on its stdout.
    """
    request = ModelClaudeCredentialProbeRequest(
        prompt=ov.prompt, model=ov.model, timeout_seconds=ov.timeout_s
    )
    script = _prelude(ov) + (
        'cd "$HOME"; K=$(mktemp); unset ANTHROPIC_API_KEY CLAUDE_CODE_OAUTH_TOKEN\n'
        f'CP=$("$P" -m {POOL_MODULE} resolve 2>"$K"); rc=$?\n'
        'L=$(sed -n "s/.* label=\\([a-z0-9-]*\\).*/\\1/p" "$K" | tail -1); rm -f "$K"\n'
        'if [ $rc -ne 0 ] || [ -z "$CP" ]; then echo "LABEL=none"; echo "RESOLVE rc=$rc"; exit 0; fi\n'
        'echo "LABEL=$L"\n'
        f"printf '%s\\n' {shlex.quote(request.model_dump_json())} | "
        f'CLAUDE_CODE_OAUTH_TOKEN="$CP" "$P" -m {PROBE_MODULE} 2>&1 | head -c 4000; unset CP\n'
    )
    done = _run(host, script, timeout=ov.timeout_s + 60)
    out = _REDACT.sub("<redacted>", done.stdout)
    label_m = re.search(r"^LABEL=(\S+)", out, re.M)
    label = label_m.group(1) if label_m and label_m.group(1) != "none" else None
    if label is None:
        return None, "empty", out.strip().replace("\n", " ")[-160:]
    receipt = _receipt(out)
    if receipt is None:
        return label, "probe-unavailable", out.strip().replace("\n", " ")[-140:]
    return label, _verdict(ov, receipt), receipt.output.replace("\n", " ")[:140]


def _receipt(text: str) -> ModelClaudeCredentialProbeReceipt | None:
    """The receipt line the probe printed, if it printed one."""
    for line in reversed(text.splitlines()):
        if line.startswith("{"):
            try:
                return ModelClaudeCredentialProbeReceipt.model_validate_json(line)
            except ValueError:
                return None
    return None


def _verdict(ov: Overlay, receipt: ModelClaudeCredentialProbeReceipt) -> str:
    """The overlay's answer patterns first, then how the call ended."""
    if receipt.exit_cause == "launch_failed":
        return "launch-failed"
    if receipt.exit_cause == "timeout":
        return classify(ov, receipt.output, 124)
    return classify(
        ov, receipt.output, receipt.exit_code if receipt.exit_code is not None else 255
    )


def classify(ov: Overlay, text: str, rc: int) -> str:
    low = text.lower()
    for verdict in ("refused", "expired", "limited"):
        if any(p in low for p in ov.patterns[verdict]):
            return verdict
    return "ok" if rc == 0 else f"unknown-rc{rc}"


def mark(ov: Overlay, host: Host, label: str, verdict: str) -> None:
    verb = (
        f"mark-limited --label {label} --seconds {ov.limited_seconds}"
        if verdict == "limited"
        else f"mark-revoked --label {label}"
    )
    _run(
        host,
        _prelude(ov) + f'cd /tmp; "$P" -m {POOL_MODULE} {verb} >/dev/null 2>&1; true\n',
        timeout=60,
    )


def draw_order(ov: Overlay, host: Host) -> list[Token]:
    """The overlay's tokens in the order ``host`` draws them: rotated by the host's place in the host list.

    Deterministic, from the overlay alone, and distinct per host while the host list is no longer than the
    token list, so the hosts do not all spend the first token's account first.
    """
    names = [h.name for h in ov.hosts]
    shift = names.index(host.name) % len(ov.tokens) if host.name in names else 0
    return ov.tokens[shift:] + ov.tokens[:shift]


def order_pool(ov: Overlay, host: Host, labels: list[str]) -> str:
    """Set the host pool's draw order to ``labels`` first; returns a failure text, empty on success."""
    done = _run(
        host,
        _prelude(ov)
        + f'cd /tmp; "$P" -m {POOL_MODULE} reorder --labels {",".join(labels)} 2>&1\n',
        timeout=60,
    )
    return (
        ""
        if done.returncode == 0
        else _REDACT.sub("<redacted>", done.stdout + done.stderr).strip()[-200:]
    )


@dataclass
class RunState:
    """What one run learns about accounts, shared by every host it serves.

    ``limited`` maps a label whose account hit its usage limit to the fingerprint of the token that
    answered so; ``pools`` is each served host's pool as last read, so a limit found later can be marked
    on the hosts already done.
    """

    hosts: list[Host] = field(default_factory=list)
    limited: dict[str, str] = field(default_factory=dict)
    pools: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)


def _spread_limit(
    ov: Overlay,
    state: RunState,
    host: Host,
    pool: dict[str, dict[str, Any]],
    label: str,
) -> list[str]:
    """Mark ``label`` limited on ``host`` when its pool holds the limited token and has not marked it."""
    entry = pool.get(label)
    if (
        entry is None
        or entry["fingerprint"] != state.limited[label]
        or entry["limited"]
        or entry["revoked"]
    ):
        return []
    mark(ov, host, label, "limited")
    entry["limited"] = True
    return [f"{host.name}:{label}"]


def refill(
    ov: Overlay,
    host: Host,
    store: dict[str, str],
    *,
    dry_run: bool,
    state: RunState | None = None,
) -> str:
    state = state if state is not None else RunState(hosts=[host])
    before = host_pool(ov, host)
    added: list[str] = []
    for token in draw_order(ov, host):
        fp = store.get(token.label, "none")
        if fp == "none":
            continue
        have = before.get(token.label)
        if have is not None and have["fingerprint"] == fp:
            continue
        if have is None and any(t["fingerprint"] == fp for t in before.values()):
            continue  # the host holds this value under another label; the pool refuses a second copy
        if dry_run:
            added.append(f"{token.label}(would-add)")
            continue
        line = install(ov, host, token, force=have is not None)
        added.append(token.label if not line.startswith("FAILED") else line)
    pool = before if dry_run or not added else host_pool(ov, host)
    wanted = [t.label for t in draw_order(ov, host) if t.label in pool]
    order = "kept"
    if wanted and list(pool)[: len(wanted)] != wanted:
        order = f"would-reorder:{wanted[0]}" if dry_run else f"reordered:{wanted[0]}"
        if not dry_run:
            failure = order_pool(ov, host, wanted)
            if failure:
                raise ClaudePoolRefillRefusedError(
                    f"{host.name}: setting the draw order failed: {failure}"
                )
            pool = host_pool(ov, host)
    spread: list[str] = []
    if not dry_run:
        state.pools[host.name] = pool
        for label in state.limited:
            spread += _spread_limit(ov, state, host, pool, label)
    first_label: str | None = None
    tried: list[str] = []
    label, verdict, excerpt = probe(ov, host)
    first_label = label
    while (
        label is not None
        and verdict != "ok"
        and not dry_run
        and len(tried) < len(ov.tokens) + 8
    ):
        tried.append(f"{label}:{verdict}")
        if verdict not in ("refused", "expired", "limited"):
            break
        mark(ov, host, label, verdict)
        if verdict == "limited" and label in pool:
            state.limited[label] = pool[label]["fingerprint"]
            pool[label]["limited"] = True
            for other in state.hosts:
                if other.name != host.name and other.name in state.pools:
                    spread += _spread_limit(
                        ov, state, other, state.pools[other.name], label
                    )
        label, verdict, excerpt = probe(ov, host)
    after = host_pool(ov, host)
    live = sum(1 for t in after.values() if not t["revoked"] and not t["limited"])
    return (
        f"claude-pool-refill host={host.name} tokens={len(after)} usable={live} "
        f"added={','.join(added) or 'none'} order={order} first={first_label or 'none'} "
        f"marked={','.join(tried) or 'none'} limit_spread={','.join(spread) or 'none'} "
        f"now={label or 'none'} verdict={verdict}"
        + ("" if verdict == "ok" else f" excerpt={excerpt!r}")
    )


def overlay_path(config_path: str | None) -> Path:
    if config_path:
        return Path(config_path)
    root = os.environ.get("ONEX_WORKSPACE_CONFIG_ROOT", "")
    return Path(root) / OVERLAY_REL if root else OVERLAY_REL


class HandlerClaudePoolRefill:
    """Fills, probes and moves past dead tokens on the declared lab hosts."""

    def handle(
        self, request: ModelClaudePoolRefillRequest
    ) -> ModelClaudePoolRefillResult:
        try:
            ov = load_overlay(overlay_path(request.config_path))
            if request.check_config:
                return ModelClaudePoolRefillResult(
                    exit_code=0,
                    lines=(
                        f"config ok: {len(ov.tokens)} store keys under {ov.folder}, "
                        f"reader {ov.reader.name}, hosts {','.join(h.name for h in ov.hosts)}",
                    ),
                )
            wanted = set(request.hosts)
            unknown = wanted - {h.name for h in ov.hosts}
            if unknown:
                raise ClaudePoolRefillRefusedError(
                    f"not declared in {ov.path}: {sorted(unknown)}"
                )
            store = store_fingerprints(ov)
            lines = [
                f"claude-pool-store keys={len(store)} "
                f"present={sum(1 for v in store.values() if v != 'none')}"
            ]
            bad = 0
            served = [h for h in ov.hosts if not wanted or h.name in wanted]
            state = RunState(hosts=served)
            for host in served:
                try:
                    line = refill(ov, host, store, dry_run=request.dry_run, state=state)
                except (
                    ClaudePoolRefillRefusedError,
                    subprocess.TimeoutExpired,
                    ValueError,
                ) as exc:
                    detail = _REDACT.sub("<redacted>", str(exc))[:200]
                    line = f"claude-pool-refill host={host.name} verdict=error detail={detail!r}"
                bad += "verdict=ok" not in line
                lines.append(line)
            return ModelClaudePoolRefillResult(
                exit_code=0 if bad == 0 else 1, lines=tuple(lines)
            )
        except ClaudePoolRefillRefusedError as exc:
            return ModelClaudePoolRefillResult(exit_code=2, refusal=str(exc))
