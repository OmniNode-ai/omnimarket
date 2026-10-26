# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Render the Codex sandbox probe that the remote-lane runner and the placement pool both run (OMN-17427).

The runner refuses a lane (status ``codex-sandbox-unavailable``) when ``codex sandbox`` cannot take the lane's writable roots. The
pool used to judge a host by a cached ``codex exec`` smoke with three roots, so a host past the sandbox's argument ceiling read ``ok``
and kept being chosen for Codex lanes that then failed. Both now run the lines rendered here.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Mapping

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    find_node_overlay,
    load_structured_overlay,
    not_configured_message,
)

from ..models.model_codex_sandbox_probe import (
    ModelCodexSandboxProbeRequest,
    ModelCodexSandboxProbeResult,
    ModelReposPrivateData,
)

CODEX_SANDBOX_PROBE_TIMEOUT_S = 30
CODEX_SANDBOX_PROBE_LOG = "codex-sandbox-probe.log"
# A failed probe prints its log's last bytes in its own line, so the receipt names the cause after lane closeout
# removes the context directory (OMN-17427: 40 lanes said only rc=1 while the log said "Argument list too long").
CODEX_SANDBOX_PROBE_TAIL_BYTES = 400
# Exercise the complete resolved permission profile, including caches and Git metadata.
# A unique temporary file avoids clobbering another probe or a lane's existing file.
# Each failure names the step that failed: ``exists`` (the sandbox does not see the root, so it was never mounted or was removed) or
# ``write`` (the root is there and refused a file), so the pool's refusal says which (OMN-17427, two lab hosts, 2026-10-18).
CODEX_WRITE_PROBE_PROGRAM = """import json,os,sys,tempfile
def fail(step,path,exc):
 print('REMOTE-LANE-CODEX-CAPABILITY-UNAVAILABLE capability=write-path step='+step+' path='+path+' error='+str(exc),file=sys.stderr)
 sys.exit(1)
for path in json.loads(sys.argv[1]):
 if not os.path.exists(path): fail('exists',path,'no such path inside the sandbox')
 try:
  with tempfile.NamedTemporaryFile(prefix='.codex-sandbox-probe-',dir=path) as output:
   output.write(b'write capability probe'); output.flush()
 except OSError as exc:
  fail('write',path,exc)
"""
# Run outside the sandbox, right before it starts: a root that does not exist is not mounted (this host's codex refuses it with
# ``bwrap: Can't bind mount``, another's starts without it and the write then finds no directory). A probe's own directory can
# vanish between the shell's mkdir and the sandbox when another probe's exit trap removes it, so every root is created again here.
CODEX_ROOTS_ENSURE_PROGRAM = (
    "import json,os,sys\n"
    "for path in json.loads(sys.argv[1]): os.makedirs(path,exist_ok=True)\n"
)
# What a cached smoke failure depends on, as one line: the codex version, the bwrap binary and the AppArmor profiles and user-namespace
# sysctls that decide whether it may start. A failure cached against another line is ignored, so a host owner's fix (an AppArmor
# profile for /usr/bin/bwrap, a sysctl, a codex upgrade) is read by the next probe, not an hour later (OMN-17427, one lab host, 2026-10-18).
CODEX_FINGERPRINT_PROGRAM = """import os,shutil,subprocess,sys
def newest(path):
 if not os.path.exists(path): return 0
 stamp=os.stat(path).st_mtime
 for here,_,names in os.walk(path) if os.path.isdir(path) else ():
  for name in names:
   try: stamp=max(stamp,os.stat(os.path.join(here,name)).st_mtime)
   except OSError: pass
 return int(stamp)
def text(path):
 try:
  with open(path) as handle: return handle.read().strip()
 except OSError: return '-'
try: version=subprocess.run([sys.argv[1],'--version'],capture_output=True,text=True,timeout=10,check=False).stdout.strip()
except (OSError,subprocess.SubprocessError): version='unrunnable'
print('|'.join([version,str(newest(shutil.which('bwrap') or '/usr/bin/bwrap')),str(newest('/etc/apparmor.d')),
 text('/proc/sys/kernel/apparmor_restrict_unprivileged_userns'),text('/proc/sys/kernel/unprivileged_userns_clone')]))
"""
# Portable to macOS (no GNU timeout). The probe owns a process group so its descendants cannot keep
# the SSH output pipe open after a timeout. Keep diagnostics in the context directory, never discard them.
CODEX_SANDBOX_PROBE_PY = """import json,os,signal,subprocess,sys,time
log,seconds=sys.argv[1],float(sys.argv[2])
t=time.monotonic(); timed_out=False
with open(log,'wb') as output:
 p=subprocess.Popen(sys.argv[3:],stdin=subprocess.DEVNULL,stdout=output,stderr=output,start_new_session=True)
 try:
  rc=p.wait(timeout=seconds)
 except subprocess.TimeoutExpired:
  timed_out=True
  try: os.killpg(p.pid,signal.SIGTERM)
  except ProcessLookupError: pass
  time.sleep(0.2)
  try: os.killpg(p.pid,signal.SIGKILL)
  except ProcessLookupError: pass
  p.wait(); rc=124
view=dict(rc=rc,timed_out=timed_out,elapsed_s=round(time.monotonic()-t,2),log=log)
if rc:
 with open(log,'rb') as output:
  oversize=b'Argument list too long' in output.read()
 for arg in sys.argv[3:] if oversize else ():
  if arg.startswith('sandbox_workspace_write.writable_roots='):
   try:
    roots=json.loads(arg.split('=',1)[1]); by={}
    for path in roots:
     name=path.split('/.git/')[0].rsplit('/',1)[-1] if '/.git/' in path else path.rsplit('/',1)[-1]
     by[name]=by.get(name,0)+1
    top=','.join('%s:%d'%kv for kv in sorted(by.items(),key=lambda kv:-kv[1])[:4])
    with open(log,'ab') as output:
     output.write(('\\nCODEX-SANDBOX-OVERSIZE roots=%d arg_bytes=%d largest=%s'%(len(roots),len(arg),top)).encode())
   except ValueError: pass
 with open(log,'rb') as output:
  output.seek(0,2); output.seek(max(0,output.tell()-TAIL))
  view['log_tail']=output.read().decode('utf-8','replace').strip()[-TAIL:]
print('REMOTE-LANE-CODEX-SANDBOX-PROBE '+json.dumps(view))
sys.exit(rc if rc>=0 else 1)
""".replace("TAIL", str(CODEX_SANDBOX_PROBE_TAIL_BYTES))
# Extra empty writable roots the probe adds to the lane's set. The permission profile Codex hands its Linux helper is one argument
# with a 128 KiB ceiling (MAX_ARG_STRLEN) and costs about 0.6 KiB for an empty root and more for a git one; one lab host's real set measured
# 124.7 KiB of 128 on 2026-10-18, passing or failing as other lanes' worktrees came and went. Eight spare roots (about 4.5 KiB) keep a
# host that sits on the ceiling out of Codex placement instead of admitting lanes that then fail one by one.
CODEX_ROOTS_PROBE_HEADROOM = 8
# The fitter reserves this many spare empty roots (about 1.2 KB each, 12 more than the pool probe's own headroom) so the fitted
# set never sits at the ceiling. On one lab host, 2026-10-23, 109 roots sat only 2 spare roots under it and failed the probe.
CODEX_ROOTS_FIT_SPARE = 20
# A scratch cwd has no common gitdir. Git worktree add still writes objects, refs and the worktree
# registry of the host's repository clones. Grant those directories for peer clones, leaving their index,
# HEAD, config and source tree read-only. Materialize them before Codex resolves its roots (OMN-17427).
# Grant the lane's OWN common dir whole: git fetch writes FETCH_HEAD, packed-refs, shallow and gc.pid at its top (OMN-21279).
# This makes that clone's hooks and config writable inside the sandbox, so only the lane's own common dir is granted this way.
# Also grant its direct alternate objects directories, without granting the alternate clones' other metadata.
# Only the registry clones, the sibling private data clone, the lane's own common dir and its direct alternate objects are granted.
# Other peer bare caches in the runner's repository cache stay read-only: the runner fills those from outside the sandbox, and on Linux codex
# hands its whole permission profile to codex-linux-sandbox as ONE argument, about 1.1 KB per writable root,
# which the kernel refuses past 128 KiB (MAX_ARG_STRLEN). Granting all 19 caches put one lab host at 140 roots and every
# Codex lane there failed the probe with "Argument list too long (os error 7)" (OMN-17427, 2026-10-07).
# A registry entry is a repository only when its .git is a gitlink file or a directory holding HEAD. The
# Codex sandbox leaves an empty .git mount point in a writable root (an empty .git under the registry state directory on one lab host,
# 2026-10-07: every Codex lane aborted on it); such an entry is skipped and granted nothing. A .git that
# has a repository's shape and that git refuses still fails the launch, as does any bare cache entry. A gitlink file whose gitdir target
# is gone (a linked worktree of a clone that was removed: one lab host, 2026-10-22, a shared registry worktree, which took every Codex
# lane off the host) is a broken worktree: it is skipped, granted nothing and named on stderr.
# Grant the configured private data clone beside both the registry path and its resolved path: a registry
# alias and its target can each have a sibling clone, and Codex's login shell can re-export the resolved registry root.
# The registry itself can outgrow the ceiling (one lab host, 2026-10-18: 23 clones made a 133 KB profile and every Codex lane and the pool
# probe failed with the same E2BIG, with a 653-byte environment and no brief in any argument). With the codex binary and the lane's
# other roots passed, the program asks the sandbox itself whether the roots fit with the pool probe's spare roots, and when they
# do not keeps whole clones in order: the lane's own clone and the sibling internal clone, the lane's repository
# (``CODEX_ROOTS_PRIORITY``), then the rest by path. It names the dropped repositories on stderr.
# The fit leaves CODEX_ROOTS_FIT_SPARE spare roots of margin, not the pool probe's 8.
# The fit runs on Linux only, and a sandbox that does not answer within the timeout counts as fitting
# (OMN-21396, h200: the fit's 30 s timeout crashed the roots program on macOS).
# A worktree probe lane: worktree add -b failed 255 "cannot lock ref" in the ungranted clone
# (OMN-17427, 2026-10-07).
CODEX_GIT_ROOTS_PROGRAM = r"""import json,os,pathlib,shutil,subprocess,sys,tempfile
registry=pathlib.Path(sys.argv[1]); common=sys.argv[2]
cx=sys.argv[3] if len(sys.argv)>3 else ''; base=json.loads(sys.argv[4]) if len(sys.argv)>4 else []
priority=[n for n in os.environ.get('CODEX_ROOTS_PRIORITY','').split(',') if n]
beside=[n for n in os.environ.get('CODEX_ROOTS_BESIDE','').split(',') if n]
spare=int(os.environ.get('CODEX_ROOTS_SPARE','__SPARE__'))
def entry(gitpath):
 if not gitpath.exists(): return []
 if gitpath.is_file():
  gitdir=gitpath.read_text(errors='replace').strip()
  if gitdir.startswith('gitdir:'):
   target=pathlib.Path(gitdir[7:].strip())
   if not target.is_absolute(): target=gitpath.parent/target
   if not target.is_dir():
    print('CODEX-GIT-ROOTS skip broken worktree (gitdir target missing): '+str(gitpath)+' -> '+str(target),file=sys.stderr); return []
  return [gitpath]
 if (gitpath/'HEAD').is_file(): return [gitpath]
 print('CODEX-GIT-ROOTS skip non-repository: '+str(gitpath),file=sys.stderr); return []
def metadata_of(repos):
 found=[]
 for repo in repos:
  result=subprocess.run(['git','--git-dir='+str(repo),'rev-parse','--path-format=absolute','--git-common-dir'],
                        capture_output=True,text=True,check=False,timeout=10)
  if result.returncode: raise SystemExit('CODEX-GIT-ROOTS invalid repository: '+str(repo))
  found.append(pathlib.Path(result.stdout.strip()).resolve())
 return list(dict.fromkeys(found))
def label(directory): return directory.name if directory.suffix=='.git' else directory.parent.name
keep=[]
if common: keep.append(pathlib.Path(common))
for p in dict.fromkeys((registry.parent, registry.resolve().parent)):
 for name in beside: keep.extend(entry(p/name/'.git'))
rest=[]
if registry.is_dir():
 for p in sorted(registry.iterdir()): rest.extend(entry(p/'.git'))
keep=metadata_of(keep); rest=[d for d in metadata_of(rest) if d not in keep]
common=keep[0] if common else None
home=pathlib.Path.home().resolve()
def safe_root(path):
 return path!=pathlib.Path(path.anchor) and path!=home and (common is None or path not in common.parents)
own_roots=[]
if common is not None:
 if not safe_root(common): raise SystemExit('CODEX-GIT-ROOTS unsafe common dir: '+str(common))
 own_roots.append(str(common))
 alternates=common/'objects/info/alternates'
 if alternates.is_file():
  for line in alternates.read_text(errors='replace').splitlines():
   line=line.strip()
   if not line or line.startswith('#'): continue
   path=pathlib.Path(line)
   if not path.is_absolute(): path=common/'objects'/path
   try:
    path=path.resolve()
    valid=safe_root(path) and path.name=='objects' and path.is_dir() and (path.parent/'HEAD').is_file()
   except (OSError,RuntimeError,ValueError): valid=False
   if valid: own_roots.append(str(path))
   else: print('CODEX-GIT-ROOTS skip alternate: '+str(path),file=sys.stderr)
rest.sort(key=lambda d: (label(d) not in priority, str(d)))
units=keep+rest
def roots_of(directories):
 out=[]
 for directory in directories:
  if directory==common:
   out.extend(own_roots); continue
  for relative in ('objects','refs','logs/refs','worktrees'):
   path=directory/relative
   path=path.resolve()
   if not safe_root(path): raise SystemExit('CODEX-GIT-ROOTS unsafe root: '+str(path))
   path.mkdir(parents=True,exist_ok=True)
   out.append(str(path))
 return list(dict.fromkeys(out))
all_roots=roots_of(units)
# Codex hands its whole permission profile to its Linux helper as ONE argument (about 1.2 KB per writable root), which the kernel
# refuses past 128 KiB. With the codex binary and the lane's other roots given, the registry clones are fitted to that ceiling by
# the sandbox itself: the lane's own clone, the sibling internal clone and the lane's repository come first, the rest by name.
def fits(candidate):
 # The spare roots are named like the pool probe's (a sibling of the context directory, headroom-N), so they cost as much.
 pad=(base[1].rstrip('/')+'.fit') if len(base)>1 else tempfile.mkdtemp(prefix='codex-pad-')
 try:
  extra=[]
  for i in range(spare):
   os.makedirs(pad+'/headroom-%d'%i,exist_ok=True); extra.append(pad+'/headroom-%d'%i)
  done=subprocess.run([cx,'sandbox','-c','sandbox_mode="workspace-write"','-c','sandbox_workspace_write.network_access=true',
                       '-c','sandbox_workspace_write.writable_roots='+json.dumps(base+candidate+extra),'--','true'],
                      stdin=subprocess.DEVNULL,capture_output=True,text=True,check=False,timeout=30)
  return 'Argument list too long' not in done.stdout+done.stderr
 except subprocess.TimeoutExpired: return True
 finally: shutil.rmtree(pad,ignore_errors=True)
if cx and base and sys.platform.startswith('linux') and not fits(all_roots):
 low,high=len(keep),len(units)
 while low<high:
  mid=(low+high+1)//2
  if fits(roots_of(units[:mid])): low=mid
  else: high=mid-1
 print('CODEX-GIT-ROOTS profile budget: kept %d of %d repositories; dropped: %s'%(
       low,len(units),', '.join(label(d) for d in units[low:])),file=sys.stderr)
 all_roots=roots_of(units[:low])
print(json.dumps(all_roots))
""".replace("__SPARE__", str(CODEX_ROOTS_FIT_SPARE))


# The host's own tool caches, wherever its login profile puts them (including separate uv and pre-commit caches). Codex runs
# its commands in a login shell, so they read the same values, and both the remote-lane runner and the landing worker grant them.
TOOL_CACHE_VARS = ("UV_CACHE_DIR", "PRE_COMMIT_HOME", "XDG_CACHE_HOME")


REGISTRY_ROOT_VAR = "ONEX_REGISTRY_ROOT"


def _validate_registry_root_var(name: str) -> None:
    """Accept a shell variable name without allowing shell syntax."""
    if re.fullmatch(r"[A-Z_][A-Z0-9_]*", name) is None:
        raise ValueError("registry_root_var must match [A-Z_][A-Z0-9_]*")


def _private_data_name(
    private_data: str | None, environ: Mapping[str, str] | None
) -> str:
    """Resolve the private data clone name explicitly or from the shared repos overlay."""
    if private_data is None:
        overlay = (
            load_structured_overlay("repos", ModelReposPrivateData, environ)
            if find_node_overlay("repos", environ) is not None
            else None
        )
        if overlay is None or overlay.private_data is None:
            where = find_node_overlay("repos", environ)
            raise NodeOverlayError(
                (f"{where}: " if where else "")
                + not_configured_message("repos", "repos.private_data")
            )
        private_data = overlay.private_data
    if re.fullmatch(r"[A-Za-z0-9._-]+", private_data) is None:
        raise ValueError("private_data must be non-empty and match [A-Za-z0-9._-]+")
    return private_data


def codex_roots_lines(
    repo: str = "",
    *,
    private_data: str | None = None,
    registry_root_var: str = REGISTRY_ROOT_VAR,
    environ: Mapping[str, str] | None = None,
) -> list[str]:
    """The shell lines that build ``CXR`` (the JSON list of writable roots a Codex lane's sandbox is given) from ``$W`` (the
    worktree), ``$C`` (the context directory), ``$GC`` (the worktree's git common dir or empty), ``$ONEX_REGISTRY_ROOT``,
    ``$ONEX_STATE_DIR``, ``$ONEX_WORKTREES_ROOT`` and ``$ONEX_LANE``. The remote-lane runner and the pool probe (``PREFLIGHT_SH``)
    both run exactly these lines, so the probe sees the root count a lane will have (OMN-17427: one lab host's registry grew past the
    sandbox's argument limit while the pool's own smoke, run with three roots, kept saying ``ok``). ``repo`` is the lane's
    repository: the registry clone the profile budget keeps first when the registry outgrows the ceiling."""
    _validate_registry_root_var(registry_root_var)
    name = _private_data_name(private_data, environ)
    return [
        *([f"export CODEX_ROOTS_PRIORITY={shlex.quote(repo)}"] if repo else []),
        # OMN-21396: uv's managed Pythons and tool envs need this data directory (h101:
        # mkdir under ~/.local/share/uv returned "Operation not permitted"); grant no parent.
        'mkdir -p "$HOME/.cache" "$HOME/.local/share/uv" "$ONEX_STATE_DIR" || exit 98',
        # A scratch lane creates registry worktrees here. Materialize its directory before resolving
        # roots, including on one lab host's symlinked registry, without granting the whole registry or peers.
        'mkdir -p "$ONEX_WORKTREES_ROOT/$ONEX_LANE" || exit 98',
        # What every lane's delegation step writes, and the sandbox otherwise refuses: the receipt directory
        # under the host's registry (delegation_step names the lane per call, so the whole directory) and the
        # directory of the local delegation sqlite, which the typed-decision node opens read-write (its
        # journal files need the directory, not only the file). Both are created here so a root is never
        # dropped for not existing yet.
        f'mkdir -p "${registry_root_var}/.claude_scratch/delegation-receipts" "$HOME/.omninode/delegation" || exit 98',
        # omnibase_infra's lane_lock fixes its lock directory here (every process must agree on one path), and a governed refresh
        # opens its lock file there: only that directory, never its parent state directory (OMN-17427, one lab host, 2026-10-21: a Codex lane
        # ended blocked on "Read-only file system" at lane-locks/omnibase-infra.lock).
        'mkdir -p "$HOME/.omnibase/state/lane-locks" || exit 98',
        'CXR="[\\"$W\\",\\"$C\\",\\"$HOME/.cache\\",\\"$HOME/.local/share/uv\\",\\"$ONEX_STATE_DIR\\",'
        f'\\"$ONEX_WORKTREES_ROOT/$ONEX_LANE\\",\\"${registry_root_var}/.claude_scratch/delegation-receipts\\",'
        '\\"$HOME/.omninode/delegation\\",\\"$HOME/.omnibase/state/lane-locks\\"${GC:+,\\"$GC\\"}"',
        # The host's own tool caches (TOOL_CACHE_VARS).
        f'for v in {" ".join(TOOL_CACHE_VARS)}; do p=$(bash -lc "printf %s \\"\\${{$v:-}}\\"" '
        '</dev/null) || { echo "REMOTE-LANE-CODEX-CAPABILITY-UNAVAILABLE capability=$v profile-read-failed"; exit 96; }; '
        '[ -n "$p" ] || continue; mkdir -p "$p" || { '
        'echo "REMOTE-LANE-CODEX-CAPABILITY-UNAVAILABLE capability=$v path=$p"; exit 96; }; '
        'CXR="$CXR,\\"$p\\""; done',
        '[ -d "$HOME/.onex" ] && CXR="$CXR,\\"$HOME/.onex\\""',
        'CXR="$CXR]"',
        # Codex refuses writable roots with any symlink component (one lab host's registry alias). Pass the
        # same directories by their real paths; the sandbox's permissions and exclusions stay intact.
        "CXR=$(python3 -c 'import json,pathlib,sys; "
        "print(json.dumps(list(dict.fromkeys(str(pathlib.Path(p).resolve()) "
        'for p in json.loads(sys.argv[1])))))\' "$CXR") || exit 98',
        f"export CODEX_ROOTS_BESIDE={shlex.quote(name)}",
        f'GXR=$(python3 -c {shlex.quote(CODEX_GIT_ROOTS_PROGRAM)} "${registry_root_var}" "$GC" "${{CX:-}}" "$CXR") '
        '|| { echo "REMOTE-LANE-CODEX-GIT-ROOTS-FAILED"; exit 98; }',
        "CXR=$(python3 -c 'import json,sys; print(json.dumps(list(dict.fromkeys("
        'json.loads(sys.argv[1])+json.loads(sys.argv[2])))))\' "$CXR" "$GXR") || exit 98',
    ]


def landing_worker_roots_lines(
    *, registry_root_var: str = REGISTRY_ROOT_VAR
) -> list[str]:
    """The shell lines that build ``CX_ROOT_ARGS``, the ``--add-dir`` arguments of a landing worker's ``codex exec``, each root by its
    real path (Codex refuses a writable root with a symlink component): ``$C`` (the worker's context directory), ``$R`` (the bare
    repository), ``$GD`` (the worktree's own git directory), the home caches, the delegation state, and each tool cache the host's
    login profile names (``TOOL_CACHE_VARS``), created first. OMN-19998 (one lab host, 2026-10-18): the worker granted only the home cache root, so ``uv`` could not start the focused tests ("Read-only file system (os
    error 30) at the configured uv cache") and the worker ended blocked with its conflict resolution staged. Exit 98 when the login
    profile cannot be read or a cache cannot be created."""
    _validate_registry_root_var(registry_root_var)
    return [
        'CX_ROOT_ARGS=(); for d in "$C" "$R" "$GD" "$HOME/.cache" "$HOME/.local/share" ${ONEX_STATE_DIR:+"$ONEX_STATE_DIR"} '
        f'"${registry_root_var}/.claude_scratch/delegation-receipts" "$HOME/.omninode/delegation"; do '
        'r=$(cd "$d" 2>/dev/null && pwd -P) || r="$d"; CX_ROOT_ARGS+=(--add-dir "$r"); done',
        f'for v in {" ".join(TOOL_CACHE_VARS)}; do p=$(bash -lc "printf %s \\"\\${{$v:-}}\\"" </dev/null) || exit 98; '
        '[ -n "$p" ] || continue; mkdir -p "$p" || exit 98; r=$(cd "$p" && pwd -P) || exit 98; CX_ROOT_ARGS+=(--add-dir "$r"); done',
    ]


def codex_sandbox_probe_line() -> str:
    """The shell line that proves every root in ``$CXR`` writable under the Codex sandbox (``$CX`` the codex binary, ``$C`` the
    log's directory). It exits nonzero when the sandbox cannot start or cannot write a root; the caller names the failure."""
    return (
        f'python3 -c {shlex.quote(CODEX_ROOTS_ENSURE_PROGRAM)} "$CXR" || exit 98; '
        f'python3 -c {shlex.quote(CODEX_SANDBOX_PROBE_PY)} "$C/{CODEX_SANDBOX_PROBE_LOG}" {CODEX_SANDBOX_PROBE_TIMEOUT_S} '
        '"$CX" sandbox -c \'sandbox_mode="workspace-write"\' -c sandbox_workspace_write.network_access=true '
        f'-c "sandbox_workspace_write.writable_roots=$CXR" -- python3 -c {shlex.quote(CODEX_WRITE_PROBE_PROGRAM)} "$CXR"'
    )


# The Codex sandbox as a lane meets it (OMN-17427). ``CODEXSANDBOX`` used to come from the cached ``codex exec`` smoke alone, which runs
# with three writable roots; the remote-lane runner gives a lane one root set per registry clone and refuses the lane (status
# ``codex-sandbox-unavailable``) when ``codex sandbox`` cannot take it. On one lab host the registry grew to 109 roots, past the sandbox's
# argument limit, so the smoke said ``ok`` while every Codex lane placed there failed, and the throughput tick kept counting 10 free
# Codex slots. The probe now also runs the runner's own roots (``codex_roots_lines``) and its own write probe
# (``codex_sandbox_probe_line``) over the host's registry root, in a bash subshell bounded by ``CODEX_ROOTS_PROBE_TIMEOUT_S``. A
# pass is kept ten minutes and a failure two, with its reason (``CODEXSANDBOXWHY``, read as ``Reading.codex_sandbox_why``).
CODEX_ROOTS_PROBE_TIMEOUT_S = 90
CODEX_HEADROOM_PROGRAM = (
    "import json,os,sys\n"
    "roots=json.loads(sys.argv[1])\n"
    "for i in range(int(sys.argv[3])):\n"
    " p=os.path.join(sys.argv[2],'headroom-%d'%i); os.makedirs(p,exist_ok=True); roots.append(os.path.realpath(p))\n"
    "print(json.dumps(roots))\n"
)


def codex_roots_probe_sh(
    *, private_data: str | None = None, environ: Mapping[str, str] | None = None
) -> str:
    """Render the self-contained roots probe without reading deployment state on import."""
    return "\n".join(
        [
            # One directory per probe: with a shared name, one probe's exit trap removed the directory another was about to write into
            # (one lab host, 2026-10-18: ``[Errno 2] No such file or directory: '.../codex-sandbox-probe/.codex-sandbox-probe-<rand>'``).
            'ONEX_REGISTRY_ROOT=$1; ONEX_STATE_DIR="$ONEX_REGISTRY_ROOT/.onex_state"; ONEX_WORKTREES_ROOT="$ONEX_REGISTRY_ROOT/omni_worktrees"; '
            "ONEX_LANE=codex-sandbox-probe-$$",
            'CX=$(command -v codex 2>/dev/null || true); [ -n "$CX" ] || { [ -x "$HOME/.local/bin/codex" ] && CX="$HOME/.local/bin/codex"; }',
            '[ -n "$CX" ] || exit 94',
            'W="$HOME/lab-run"; mkdir -p "$W" || exit 98',
            'C=$(mktemp -d "$W/.sandbox-roots.XXXXXX") || exit 98',
            'trap \'rm -rf "$C" "$C.pad"; rmdir "$ONEX_WORKTREES_ROOT/$ONEX_LANE" 2>/dev/null\' EXIT',
            # A lane's worktree has a common dir under the bare caches; give the probe one so its root count matches.
            'GC=""; for g in "$HOME"/lab-run/repos/*.git; do [ -d "$g" ] && GC=$g && break; done',
            *codex_roots_lines(private_data=private_data, environ=environ),
            # Headroom: a lane's own worktree adds entries to the profile, and the registry grows between this read and the lane.
            f'CXR=$(python3 -c {shlex.quote(CODEX_HEADROOM_PROGRAM)} "$CXR" "$C.pad" {CODEX_ROOTS_PROBE_HEADROOM}) || exit 98',
            codex_sandbox_probe_line(),
        ]
    )


# The Codex half of the pool probe: login, the cached smoke, and the lane-shaped roots probe. Placeholders are replaced in ``handle``.
PREFLIGHT_CODEX_SH = r"""
if [ -n "$SSH_CLIENT" ] || [ -n "$SSH_CONNECTION" ] || [ -n "${PROBE_CODEX_LOCAL:-}" ]; then
  cl=0; cs=unknown
  if command -v codex >/dev/null 2>&1; then probe_bound __TOOL_S__ codex login status >/dev/null 2>&1; crc=$?; else crc=1; fi
  if [ "$crc" -ge 124 ]; then cl=unavailable
  elif [ "$crc" -eq 0 ]; then
    cl=1
    K="$HOME/.cache/omni"; mkdir -p "$K"
    FP=$(python3 -c __FP_PROGRAM__ "$(command -v codex)" 2>/dev/null </dev/null) || FP=unreadable
    if [ -n "$(find "$K/codex-sandbox-ok" -mmin -360 2>/dev/null)" ]; then cs=ok
    elif [ -n "$(find "$K/codex-sandbox-fail" -mmin -60 2>/dev/null)" ] && [ "$(sed -n 1p "$K/codex-sandbox-fail")" = "$FP" ]; then
      cs=fail; cwhy=$(sed -n 2p "$K/codex-sandbox-fail")
    elif [ -z "$(find "$K/codex-sandbox-check" -mmin -10 2>/dev/null)" ]; then
      touch "$K/codex-sandbox-check"
      nohup sh -c '__BOUND_FN__
        d="$HOME/lab-run/.sandbox-probe"; rm -rf "$d"; mkdir -p "$d/c" "$d/seed" || exit 0
        g="git -c user.email=probe@probe -c user.name=probe"
        $g -C "$d/seed" init -q && $g -C "$d/seed" commit -q --allow-empty -m seed && $g -C "$d/seed" worktree add -q --detach "$d/w" HEAD || exit 0
        gd=$(git -C "$d/w" rev-parse --absolute-git-dir)
        probe_bound __SMOKE_S__ codex exec -C "$d/w" --skip-git-repo-check --ephemeral --ignore-user-config -c model_reasoning_effort="low" -s workspace-write --add-dir "$d/c" --add-dir "$d/seed/.git" --add-dir "$gd" "Run exactly: touch ok-w && git add ok-w && git -c user.email=probe@probe -c user.name=probe commit -q -m ok && touch ../c/ok-c. Then reply with the single word done." >"$d/smoke.log" 2>&1 </dev/null; src=$?
        if [ "$src" -ge 124 ]; then :
        elif [ -f "$d/c/ok-c" ] && [ "$(git -C "$d/w" log -1 --format=%s 2>/dev/null)" = ok ]; then touch "$0/codex-sandbox-ok"; rm -f "$0/codex-sandbox-fail"
        else
          # The marker is the fingerprint it ran against, then what the smoke said, so a refusal names its cause (one lab host had none).
          { printf "%s\n" "$1"; printf "smoke rc=%s %s\n" "$src" "$(tail -c 300 "$d/smoke.log" 2>/dev/null | tr "\n\t" "  ")"; } > "$0/codex-sandbox-fail"
        fi
        rm -rf "$d"' "$K" "$FP" >/dev/null 2>&1 </dev/null &
    fi
    if [ -n "${PARITY_H:-}" ]; then
      if [ -n "$(find "$K/codex-roots-ok" -mmin -10 2>/dev/null)" ]; then :
      elif [ -n "$(find "$K/codex-roots-fail" -mmin -2 2>/dev/null)" ]; then cs=fail; cwhy=$(cat "$K/codex-roots-fail" 2>/dev/null)
      else
        rout=$(probe_bound __ROOTS_S__ bash -c __ROOTS_PROBE__ codex-roots "$PARITY_H" 2>&1 </dev/null); rrc=$?
        if [ "$rrc" -eq 0 ]; then touch "$K/codex-roots-ok"; rm -f "$K/codex-roots-fail"
        else
          cs=fail
          cwhy=$(printf '%s\n' "$rout" | sed -n 's/.*"log_tail": "\(.*\)"}.*/\1/p' | tail -n 1 | tr '\n\t' '  ')
          [ -n "$cwhy" ] || cwhy=$(printf %s "$rout" | tail -c 200 | tr '\n\t' '  ')
          cwhy="rc=$rrc $cwhy"
          printf %s "$cwhy" > "$K/codex-roots-fail"; rm -f "$K/codex-roots-ok"
        fi
      fi
    fi
  fi
  echo "CODEXLOGIN $cl"
  echo "CODEXSANDBOX $cs"
  [ -z "${cwhy:-}" ] || echo "CODEXSANDBOXWHY $cwhy"
fi
"""


class HandlerCodexSandboxProbe:
    def handle(
        self, request: ModelCodexSandboxProbeRequest
    ) -> ModelCodexSandboxProbeResult:
        script = (
            PREFLIGHT_CODEX_SH.replace("__ROOTS_S__", str(CODEX_ROOTS_PROBE_TIMEOUT_S))
            .replace(
                "__ROOTS_PROBE__",
                shlex.quote(codex_roots_probe_sh(private_data=request.private_data)),
            )
            .replace("__FP_PROGRAM__", shlex.quote(CODEX_FINGERPRINT_PROGRAM))
            .replace("__TOOL_S__", str(request.tool_timeout_s))
            .replace("__SMOKE_S__", str(request.smoke_timeout_s))
            .replace("__BOUND_FN__", request.bound_fn)
        )
        return ModelCodexSandboxProbeResult(preflight_codex_sh=script)
