# OMN-11011 Migration Package Classification

Date: 2026-05-16

Scope: classify the omnimarket package migrations for `nightly_loop_decisions`, `nightly_loop_iterations`, and `review_bot_bypass_log` after generated-tree exclusion. `node_service_registry` is intentionally out of scope for OMN-11011.

## Result

The three OMN-11011 tables are package-owned omnimarket migrations and do not require suppressions or repairs after generated-tree exclusion:

| Table | Owner migration | Scanned definitions | Conflict status |
| --- | --- | ---: | --- |
| `nightly_loop_decisions` | `src/omnimarket/nodes/node_nightly_loop_controller/migrations/001_create_nightly_loop_tables.sql` | 1 | No conflict |
| `nightly_loop_iterations` | `src/omnimarket/nodes/node_nightly_loop_controller/migrations/001_create_nightly_loop_tables.sql` | 1 | No conflict |
| `review_bot_bypass_log` | `src/omnimarket/nodes/node_pr_review_bot/migrations/001_create_review_bot_bypass_log.sql` | 1 | No conflict |

## Reproduction

Raw standard-repo scan:

```bash
"$OMNI_HOME/onex_change_control/.venv/bin/check-migration-conflicts" \
  --repos-root "$OMNI_HOME" \
  --repos omnibase_core omnibase_infra omniclaude omnidash omniintelligence omnimarket omnimemory onex_change_control
```

The raw scan reported 31 historical conflicts, none for the three OMN-11011 tables.

Suppressions-aware standard-repo scan:

```bash
"$OMNI_HOME/onex_change_control/.venv/bin/check-migration-conflicts" \
  --repos-root "$OMNI_HOME" \
  --repos omnibase_core omnibase_infra omniclaude omnidash omniintelligence omnimarket omnimemory onex_change_control \
  --suppressions-file "$OMNI_HOME/onex_change_control/migration_conflict_suppressions.yaml"
```

Output:

```text
No migration conflicts found.
Suppressed 31 known conflict(s) via suppressions file.
```

Suppressions-aware scan using the full canonical `.git` repo list that
omnimarket D3 resolves from `$OMNI_HOME`:

```bash
python - <<'PY'
from pathlib import Path
import os
import subprocess

root = Path(os.environ["OMNI_HOME"])
repos = [
    p.name
    for p in sorted(root.iterdir())
    if p.is_dir()
    and not p.name.startswith(".")
    and p.name != "omni_worktrees"
    and (p / ".git").exists()
]
cmd = [
    str(root / "onex_change_control" / ".venv" / "bin" / "check-migration-conflicts"),
    "--repos-root",
    str(root),
    "--repos",
    *repos,
    "--suppressions-file",
    str(root / "onex_change_control" / "migration_conflict_suppressions.yaml"),
]
result = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=120)
print(result.stdout, end="")
print(result.stderr, end="")
print("EXIT_CODE:", result.returncode)
PY
```

Output:

```text
Found 2 migration conflict(s):

  NAME_CONFLICT: table `schema_migrations`
    - omniclaude: 001_create_delegation_tables.sql (3 columns)
    - omninode_infra: 00000000_migrations_tracking.sql (8 columns)

  EXACT_DUPLICATE: table `plan_entitlements`
    - omninode_infra: 20251212_m4_plan_metrics.sql (8 columns)
    - omninode_infra: 20260429_plan_entitlements.sql (8 columns)

Suppressed 31 known conflict(s) via suppressions file.
EXIT_CODE: 1
```

Those remaining full-workspace conflicts are outside the OMN-11011 ownership
fence. They are not `nightly_loop_decisions`, `nightly_loop_iterations`, or
`review_bot_bypass_log`, and no OMN-11011 suppression is required.

Target-table scan using the OCC scanner implementation:

```bash
uv run python - <<'PY'
from pathlib import Path
import importlib.util, sys
import os

root = Path(os.environ["OMNI_HOME"])
module_path = root / "onex_change_control/src/onex_change_control/scripts/check_migration_conflicts.py"
spec = importlib.util.spec_from_file_location("check_migration_conflicts", module_path)
mod = importlib.util.module_from_spec(spec)
sys.modules["check_migration_conflicts"] = mod
assert spec.loader is not None
spec.loader.exec_module(mod)

repos = [
    p.name
    for p in sorted(root.iterdir())
    if p.is_dir()
    and not p.name.startswith(".")
    and p.name != "omni_worktrees"
    and (p / ".git").exists()
]
targets = {"nightly_loop_decisions", "nightly_loop_iterations", "review_bot_bypass_log"}
found = {table: [] for table in targets}
for sql_file in mod.find_migration_files(root, repos):
    repo_name = sql_file.relative_to(root).parts[0]
    for table in mod.extract_tables_from_sql(sql_file, repo_name):
        if table.table_name in targets:
            found[table.table_name].append(table)

for table in sorted(targets):
    print(table, len(found[table]))
PY
```

Observed counts:

```text
nightly_loop_decisions 1
nightly_loop_iterations 1
review_bot_bypass_log 1
```

## Validator Evidence

```bash
uv run pytest tests/test_migration_ownership_omn11011.py -q
uv run ruff check tests/test_migration_ownership_omn11011.py
uv run pytest tests/test_golden_chain_duplication_sweep.py -q
```
