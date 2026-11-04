# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The parity observations the pool probe runs on a host under its own python3 (OMN-20266)."""

PARITY_PY = r'''import glob, hashlib, json, os, posixpath, re, socket, subprocess, sys, threading, time
root, clones, plugins, plugin_repo = sys.argv[1:5]
bus_lane = sys.argv[5] if len(sys.argv) > 5 else ""
user_home = os.path.expanduser("~")

# RSS in KiB on macOS and Linux. Missing, denied or timed out reads remain unknown.
fseventsd_rss = "unreadable"
try:
    rss_read = subprocess.run(["ps", "-axo", "rss=,comm="], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=3)
    if rss_read.returncode == 0:
        rss_values = []
        for row in rss_read.stdout.decode("utf-8", "replace").splitlines():
            fields = row.split(None, 1)
            if len(fields) == 2 and fields[0].isdigit() and os.path.basename(fields[1]) == "fseventsd":
                rss_values.append(int(fields[0]))
        if rss_values:
            fseventsd_rss = str(sum(rss_values))
except (OSError, subprocess.SubprocessError):
    pass
print("PARITY-FSEVENTSD-RSS-KIB " + fseventsd_rss)


def git(repo, *args):
    try:
        done = subprocess.run(["git", "-C", repo] + list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.decode("utf-8", "replace").strip() if done.returncode == 0 else None


def clean(text):
    return "_".join(str(text).split()) or "-"


__CLONE_PARITY_PY__

commit = "none"
if root:
    pattern = os.path.join(root, ".onex-dispatch-venv/lib/python*/site-packages/omnimarket-*.dist-info/direct_url.json")
    for found in sorted(glob.glob(pattern)):
        try:
            commit = json.load(open(found)).get("vcs_info", {}).get("commit_id") or commit
        except (OSError, ValueError, AttributeError):
            pass
print("PARITY-VENV " + commit)

# The broker host of the host's ledger bus lane, resolved with getaddrinfo (2026-10-13: h101 lost Tailscale DNS while
# ``tailscale status`` read healthy). The broker is the lane's ``broker:`` line under ``lanes:`` in the registry's
# omnimarket/config/ci_bus_lanes.yaml; a value that is not host:port (``inmemory``, ``from-secret``) has no host to resolve.
BUS_RESOLVE_S = 5


def bus_broker(lane):
    try:
        lines = open(os.path.join(root, "omnimarket", "config", "ci_bus_lanes.yaml"), encoding="utf-8").read().splitlines()
    except OSError:
        return None
    in_lanes = in_lane = False
    for line in lines:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line.startswith(" "):
            in_lanes, in_lane = line.startswith("lanes:"), False
            continue
        key = re.match(r"^  ([A-Za-z0-9_.-]+):\s*(#.*)?$", line)
        if in_lanes and key:
            in_lane = key.group(1) == lane
            continue
        value = re.match(r"^    broker:\s*(.*)$", line)
        if in_lanes and in_lane and value:
            text = value.group(1).strip()
            if text.startswith(('"', "'")):
                return text[1:].split(text[0], 1)[0]
            return text.split(" #", 1)[0].strip()
    return None


def bus_resolves(host):
    answer = []

    def work():
        try:
            socket.getaddrinfo(host, None)
            answer.append("resolved")
        except (OSError, UnicodeError) as exc:
            answer.append(clean(type(exc).__name__ + " " + str(exc)))

    lookup = threading.Thread(target=work)
    lookup.daemon = True
    lookup.start()
    lookup.join(BUS_RESOLVE_S)
    return answer[0] if answer else "timeout after %ds" % BUS_RESOLVE_S


if bus_lane and root:
    broker = bus_broker(bus_lane)
    name = broker.rsplit(":", 1)[0] if broker and re.match(r"^[^:\s]+:[0-9]+$", broker) else ""
    if not name:
        print("PARITY-BUS %s - unknown %s" % (bus_lane, clean("no host:port broker declared for the lane (%s)" % (broker or "absent"))))
    else:
        answer = bus_resolves(name)
        print("PARITY-BUS %s %s %s %s" % (bus_lane, name, "ok" if answer == "resolved" else "unresolved", answer))

# pre-commit's cache database lists one checkout per hook repository (repo, ref, path). An entry whose checkout is gone or holds no
# hook manifest, or whose repository is a local path that no longer exists, cannot run (h202, 2026-10-17: an entry for a deleted
# hook repository stopped hook-preflight with exit 3).
def precommit_cache():
    base = os.environ.get("PRE_COMMIT_HOME") or os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.join(user_home, ".cache"), "pre-commit")
    db = os.path.join(base, "db.sqlite")
    if not os.path.isfile(db):
        return "none", "no cache database at %s" % db
    try:
        import sqlite3
        conn = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=5)
        try:
            rows = conn.execute("select repo, ref, path from repos").fetchall()
        finally:
            conn.close()
    except Exception as exc:
        logging.getLogger(__name__).exception("Lab worker operation failed")
        return "bad", "cache database %s unreadable: %s" % (db, type(exc).__name__)
    for repo, ref, path in rows:
        local = repo[len("file://"):] if repo.startswith("file://") else repo
        if local.startswith("/") and not os.path.isdir(local):
            return "bad", "%s@%s: the local hook repository %s no longer exists" % (repo, ref, local)
        if not os.path.isdir(path) or not os.path.isfile(os.path.join(path, ".pre-commit-hooks.yaml")):
            return "bad", "%s@%s: its checkout %s is gone or holds no .pre-commit-hooks.yaml" % (repo, ref, path)
    return "ok", "%d entries" % len(rows)


verdict, detail = precommit_cache()
print("PARITY-PCCACHE %s %s" % (verdict, clean(detail)))

IGNORED_DIRS = {".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache"}
# Claude Code's runtime marker: while a session has an installed plugin loaded it writes ``.in_use/<pid>`` directly under the
# plugin's install path (read on h101, 2026-10-09: ``first=.in_use/78964``, a new pid each read). It is not shipped content, so only
# this directory at the top of the install path is left out of the comparison; a file or link of that name, or the name at any
# other depth, still counts.
RUNTIME_MARKER_DIR = ".in_use"


def blob(data):
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def installed_tree(top):
    out = {}
    for here, dirs, files in os.walk(top):
        for d in [d for d in dirs if os.path.islink(os.path.join(here, d))]:
            files.append(d)
        dirs[:] = [d for d in dirs if d not in IGNORED_DIRS and not os.path.islink(os.path.join(here, d))
                   and not (here == top and d == RUNTIME_MARKER_DIR)]
        for f in files:
            full = os.path.join(here, f)
            if f == ".DS_Store" or f.endswith(".pyc"):
                continue
            data = os.readlink(full).encode() if os.path.islink(full) else open(full, "rb").read()
            out[os.path.relpath(full, top).replace(os.sep, "/")] = blob(data)
    return out


def plugin_links(repo, rev, subdir):
    """{link: repository directory} for the plugin's directory symlinks: the install copies each target in where its link was."""
    out = {}
    for line in (git(repo, "ls-tree", "-r", rev + ":" + subdir) or "").splitlines():
        meta, _, path = line.partition("\t")
        text = git(repo, "cat-file", "blob", meta.split()[2]) if meta.startswith("120000") else ""
        target = posixpath.normpath(posixpath.join(subdir, posixpath.dirname(path), text)) if text else ".."
        if target[:2] != ".." and target != "." and not (subdir + "/").startswith(target + "/") and git(repo, "cat-file", "-t", rev + ":" + target) == "tree":
            out[path] = target
    return out


def shipped_tree(repo, rev, subdir, have=()):
    """The shipped {path: blob}; a directory link the install holds as a link (a path in ``have``) stays one entry (OMN-19998)."""
    spec = rev + ":" + subdir if subdir else rev
    try:
        done = subprocess.run(["git", "-C", repo, "ls-tree", "-r", "-z", spec], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    out = {}
    for entry in done.stdout.decode("utf-8", "replace").split("\0"):
        meta, _, path = entry.partition("\t")
        fields = meta.split()
        if path and len(fields) == 3 and fields[1] == "blob":
            out[path] = fields[2]
    for link, target in plugin_links(repo, rev, subdir).items():
        if link in have:
            continue
        out.pop(link)
        out.update((link + "/" + p, b) for p, b in (shipped_tree(repo, rev, target) or {}).items())
    return out


def digest(tree):
    return hashlib.sha256("\n".join("%s\0%s" % item for item in sorted(tree.items())).encode()).hexdigest()


def merged_tip(repo):
    """The repository's merged tip (origin/HEAD, origin/main, then main; never another branch), or None."""
    origin_head = git(repo, "symbolic-ref", "-q", "refs/remotes/origin/HEAD")
    for ref in ([origin_head] if origin_head else []) + ["refs/remotes/origin/main", "refs/heads/main"]:
        tip = git(repo, "rev-parse", "--verify", "-q", ref + "^{commit}")
        if tip:
            return tip
    return None


def merged_since(repo, rev, subdir):
    """The blobs each path of the plugin took in the commits merged after ``rev`` ({path: {blob}}), read from the repository's
    merged tip (origin/HEAD, origin/main, then main; never another branch). adopt-merged copies merged files over the install
    and leaves ``gitCommitSha`` at the revision shipped, so a copied file matches one of these and not the shipped tree
    (OMN-20862). Empty when there is no merged tip past ``rev`` or the log cannot be read: the comparison stays exact."""
    tip = merged_tip(repo)
    if not tip or git(repo, "merge-base", "--is-ancestor", rev, tip) is None:
        return {}
    log = ["log", "--raw", "--no-renames", "--no-abbrev", "-m", "-z", "--format=", rev + ".." + tip] + (["--", subdir] if subdir else [])
    raw = git(repo, *log)
    out = {}
    fields = (raw or "").split("\0")
    for meta, path in zip(fields, fields[1:]):
        parts = meta.split()
        if not meta.startswith(":") or len(parts) < 5 or parts[4] == "D":
            continue
        if subdir:
            if not path.startswith(subdir + "/"):
                continue
            path = path[len(subdir) + 1:]
        out.setdefault(path, set()).add(parts[3])
    for link, target in plugin_links(repo, rev, subdir).items():
        for path, blobs in merged_since(repo, rev, target).items():
            out.setdefault(link + "/" + path, set()).update(blobs)
    return out


# A hot-patch's backup (hotpatch-pr and adopt-merged write ``<file>.bak.<UTC stamp>`` beside the file, OMN-17427): never loaded.
BACKUP_RE = re.compile(r"^(.+)\.bak\.[0-9]{8}[^/]*$")


def loaded(path):
    """A file a unit can load: not under a ``tests`` directory (OMN-20864)."""
    return "tests" not in path.split("/")[:-1]


def plugin_line(name, state, installed, shipped, rev, detail):
    print("PARITY-PLUGIN %s %s %s %s %s %s" % (name, state, installed[:12] or "-", shipped[:12] or "-", rev[:12] or "-", clean(detail)))


wanted = [p for p in plugins.split(",") if p]
try:
    listed = json.load(open(os.path.join(user_home, ".claude/plugins/installed_plugins.json"))).get("plugins", {})
except (OSError, ValueError, AttributeError):
    listed = {}
try:
    markets = json.load(open(os.path.join(user_home, ".claude/plugins/known_marketplaces.json")))
except (OSError, ValueError):
    markets = {}
for key, entries in sorted(listed.items()):
    name, _, market = key.partition("@")
    if name not in wanted or not isinstance(entries, list):
        continue
    for entry in entries:
        rev = str(entry.get("gitCommitSha") or "")
        where = str(entry.get("installPath") or "")
        if not rev or not os.path.isdir(where):
            plugin_line(name, "unverifiable", "", "", rev, "no gitCommitSha or install path in installed_plugins.json")
            continue
        candidates = [str((markets.get(market) or {}).get("installLocation") or "")]
        candidates += sorted(glob.glob(os.path.join(user_home, "lab-run/repos/*.git")))
        candidates += [os.path.join(root, plugin_repo), os.path.join(root, "..", plugin_repo)] if root else []
        holders = [c for c in dict.fromkeys(candidates) if c and os.path.isdir(c) and git(c, "cat-file", "-e", rev + "^{commit}") is not None]
        repo = holders[0] if holders else None
        if repo is None:
            plugin_line(name, "unverifiable", "", "", rev, "revision %s is in no local clone of the plugin repository" % rev[:12])
            continue
        try:
            manifest = json.loads(git(repo, "show", rev + ":.claude-plugin/marketplace.json") or "")
            source = next(p["source"] for p in manifest["plugins"] if p["name"] == name)
        except (ValueError, KeyError, StopIteration, TypeError):
            plugin_line(name, "unverifiable", "", "", rev, "no manifest entry for %s at the shipped revision" % name)
            continue
        subdir = "" if os.path.normpath(source) == "." else os.path.normpath(source)
        have = installed_tree(where)
        shipped = shipped_tree(repo, rev, subdir, have)
        if shipped is None:
            plugin_line(name, "unverifiable", "", "", rev, "no tree for %s at the shipped revision" % source)
            continue
        tip = merged_tip(repo)
        behind, age_h, equal = "-", "-", "-"
        if tip and git(repo, "merge-base", "--is-ancestor", rev, tip) is not None:
            log = git(repo, "log", "--format=%ct", rev + ".." + tip, *(["--", subdir] if subdir else []))
            stamps = [int(t) for t in (log or "").split() if t.isdigit()]
            behind = str(len(stamps)) if log is not None else "-"
            if stamps:
                age_h = str(max(0, int((time.time() - min(stamps)) // 3600)))
                tip_tree = shipped_tree(repo, tip, subdir, have)
                if tip_tree is not None:
                    equal = "equal" if all(have.get(p) == b for p, b in tip_tree.items() if loaded(p)) and \
                        all(p in tip_tree for p in have if loaded(p) and not BACKUP_RE.match(p)) else "differs"
        print("PARITY-PLUGIN-AGE %s %s %s %s %s %s" % (name, rev[:12], (tip or "-")[:12], behind, equal, age_h))
        backups = {}
        for p in have:
            m = BACKUP_RE.match(p)
            if m and m.group(1) in have:
                backups.setdefault(m.group(1), []).append(p)
        kept = set(p for b in backups.values() for p in b)
        changed = sorted(p for p in have if p in shipped and loaded(p) and have[p] != shipped[p])
        added = sorted(p for p in have if p not in shipped and loaded(p) and p not in kept)
        removed = sorted(p for p in shipped if p not in have and loaded(p))
        merged = {}
        for holder in holders if (changed or added) else []:
            for path, blobs in merged_since(holder, rev, subdir).items():
                merged.setdefault(path, set()).update(blobs)
        adopted = [p for p in changed + added if have[p] in merged.get(p, ())]
        changed = [p for p in changed if p not in adopted]
        added = [p for p in added if p not in adopted]
        # A sanctioned hot-patch (OMN-20864): the oldest backup beside the file holds the shipped content or a merged one.
        hot = [p for p in changed if p in backups and have[sorted(backups[p])[0]] in ({shipped[p]} | merged.get(p, set()))]
        changed = [p for p in changed if p not in hot]
        if not (changed or added or removed) and hot:
            plugin_line(name, "hotpatched", digest(have), digest(shipped), rev,
                        "hotpatched=%d adopted=%d first=%s" % (len(hot), len(adopted), hot[0]))
            continue
        if not (changed or added or removed):
            plugin_line(name, "ok", digest(have), digest(shipped), rev,
                        "adopted=%d files from merged revisions since the shipped one" % len(adopted) if adopted else "tree equal")
            continue
        first = (changed + added + removed)[0]
        plugin_line(name, "modified", digest(have), digest(shipped), rev,
                    "changed=%d added=%d removed=%d first=%s" % (len(changed), len(added), len(removed), first))
'''
