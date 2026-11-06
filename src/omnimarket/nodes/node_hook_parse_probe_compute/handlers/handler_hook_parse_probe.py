# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The hook parse probe a lab host runs: render its script, without I/O (OMN-22687).

The probe runs on the host under that host's own python3 (3.6 or newer, stdlib only) and judges every hook script the host's
Claude loads with the host's own /bin/bash. A directory marketplace whose directory is missing used to read as a manifest
fault (``marketplace-unreadable:no-directory-source-for-<key>``): a lab host rebooted with no console login on 2026-11-06, its
external volume stayed unmounted, the link the marketplace path goes through dangled, and the probe blamed a manifest. The
script now names the missing volume when the path resolves onto the host's declared volumes root, and the missing directory
otherwise. The refusal is unchanged: the probe still prints ``unknown`` and the host is not placed.

The volumes root is the host's, injected by the caller from the host overlay; the script holds no volume path of its own.
"""

from __future__ import annotations

import json
import re

from ..models import ModelHookParseProbeRequest, ModelHookParseProbeScript

HOOK_PARSE_PY = r'''import json, os, re, subprocess, sys
bash, named = sys.argv[1], sys.argv[2]
home = os.path.expanduser("~")
SKIP_DIRS = {".git", ".venv", "__pycache__", "node_modules"}
VOLUMES_ROOT = __VOLUMES_ROOT__


def clean(text, limit=200):
    return ("_".join(str(text).split()) or "-")[:limit]


def load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


try:
    listed = load(os.path.join(home, ".claude/plugins/installed_plugins.json")).get("plugins") or {}
    enabled = load(os.path.join(home, ".claude/settings.json")).get("enabledPlugins") or {}
    markets = load(os.path.join(home, ".claude/plugins/known_marketplaces.json"))
except (OSError, ValueError, AttributeError) as exc:
    print("HOOKPARSE unknown plugin-config-unreadable:" + clean(exc, 120))
    sys.exit(0)


def missing_reason(top):
    """Why a directory marketplace has no directory: its link points onto a volume that is not mounted (a reboot with no console
    login leaves an external volume unmounted), or the path is simply not there. Only a volume under the host's declared
    volumes root can be named; with none declared the reason names the path."""
    root = (VOLUMES_ROOT or "").rstrip(os.sep)
    real = os.path.realpath(top)
    if root and real.startswith(root + os.sep):
        volume = real[len(root) + 1 :].split(os.sep)[0]
        if not os.path.isdir(os.path.join(root, volume)):
            return "volume-not-mounted-" + root + "/" + volume
    return "directory-source-missing-" + top


def directory_tree(key):
    """A plugin of a ``directory`` marketplace is loaded from the marketplace's own checkout, not from its cache copy (a lab host,
    2026-10-11: the cached onex parsed, the clone it is loaded from did not). None: not a directory marketplace."""
    name, _, market = key.partition("@")
    row = markets.get(market) if isinstance(markets, dict) else None
    source = row.get("source") if isinstance(row, dict) else None
    if not isinstance(source, dict) or source.get("source") != "directory":
        return None
    top = str(row.get("installLocation") or source.get("path") or "")
    if not os.path.isdir(top):
        raise ValueError(missing_reason(top))
    manifest = load(os.path.join(top, ".claude-plugin/marketplace.json"))
    rel = next((p.get("source") for p in manifest.get("plugins") or () if isinstance(p, dict) and p.get("name") == name), None)
    if not isinstance(rel, str):
        raise ValueError("no-directory-source-for-" + key)
    return os.path.realpath(os.path.join(top, rel))


trees = []
try:
    for key, entries in sorted(listed.items()) if isinstance(listed, dict) else []:
        if isinstance(enabled, dict) and enabled.get(key) is False:
            continue
        for entry in entries if isinstance(entries, list) else []:
            where = str((entry or {}).get("installPath") or "") if isinstance(entry, dict) else ""
            if where and os.path.isdir(where):
                trees.append((key, where))
        loaded = directory_tree(key)
        if loaded is not None:
            if not os.path.isdir(loaded):
                raise ValueError("directory-source-missing-" + loaded)
            trees.append((key + ":directory", loaded))
except (OSError, ValueError, AttributeError) as exc:
    print("HOOKPARSE unknown marketplace-unreadable:" + clean(exc, 160))
    sys.exit(0)
for rel in [r for r in named.split(",") if r]:
    where = os.path.join(home, rel)
    if os.path.isdir(where):
        trees.append((rel, where))
files = []
for name, top in trees:
    hooks = os.path.join(top, "hooks")
    for here, dirs, names in os.walk(hooks):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        files += [(name, top, os.path.join(here, f)) for f in sorted(names) if f.endswith(".sh")]
    try:
        with open(os.path.join(hooks, "hooks.json")) as f:
            spec = f.read()
    except OSError:
        spec = ""
    for rel in sorted(set(re.findall(r"\$\{?CLAUDE_PLUGIN_ROOT\}?/([A-Za-z0-9_./-]+\.sh)", spec))):
        full = os.path.normpath(os.path.join(top, rel))
        if os.path.isfile(full) and not full.startswith(hooks + os.sep):
            files.append((name, top, full))
bad = []
for name, top, path in files:
    try:
        with open(path, "rb") as f:
            first = f.readline().decode("utf-8", "replace").strip()
    except OSError as exc:
        print("HOOKPARSE unknown unreadable:" + clean(os.path.relpath(path, top)) + ":" + clean(exc, 80))
        sys.exit(0)
    if first.startswith("#!") and not re.search(r"(^|[/ ])(ba)?sh(\s|$)", first[2:]):
        continue
    try:
        done = subprocess.run([bash, "-n", path], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
    except subprocess.TimeoutExpired:
        print("HOOKPARSE unknown bash-n-timed-out:" + clean(os.path.relpath(path, top)))
        sys.exit(0)
    except OSError as exc:
        print("HOOKPARSE unknown " + clean(bash) + "-did-not-run:" + clean(exc, 80))
        sys.exit(0)
    if done.returncode != 0:
        err = done.stderr.decode("utf-8", "replace").strip().splitlines() or ["exit %d" % done.returncode]
        bad.append((name, os.path.relpath(path, top), err[0].replace(path + ": ", "")))
if bad:
    name, rel, err = bad[0]
    print("HOOKPARSE fail %d %s:%s %s" % (len(bad), clean(name), clean(rel), clean(err)))
else:
    print("HOOKPARSE ok %d %d" % (len(files), len(trees)))
'''

HOOK_PARSE_SH = r"""
if [ ! -x /bin/bash ]; then echo "HOOKPARSE unknown no-/bin/bash"
elif ! command -v python3 >/dev/null 2>&1; then echo "HOOKPARSE unknown no-python3"
else
python3 - /bin/bash "__HOOK_TREES__" <<'HOOKPARSE_PY_EOF'
__HOOKPARSE_PY__
HOOKPARSE_PY_EOF
hrc=$?; [ "$hrc" -eq 0 ] || echo "HOOKPARSE unknown the-hook-parse-check-exited-$hrc"
fi
"""


class HandlerHookParseProbe:
    """Render the probe script for one host's plugins and trees; never run it."""

    def handle(self, request: ModelHookParseProbeRequest) -> ModelHookParseProbeScript:
        pattern = re.compile(request.tree_pattern)
        trees = tuple(
            dict.fromkeys(
                tree.strip("/")
                for tree in request.hook_trees
                if pattern.match(tree) and ".." not in tree.split("/")
            )
        )
        volumes_root = (
            "None" if request.volumes_root is None else json.dumps(request.volumes_root)
        )
        body = HOOK_PARSE_PY.replace("__VOLUMES_ROOT__", volumes_root)
        script = HOOK_PARSE_SH.replace("__HOOK_TREES__", ",".join(trees)).replace(
            "__HOOKPARSE_PY__", body
        )
        return ModelHookParseProbeScript(script=script, hook_trees=trees)
