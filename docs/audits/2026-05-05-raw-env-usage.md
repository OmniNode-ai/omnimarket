# Raw Env-Var Usage Audit — 2026-05-05

**Ticket:** OMN-10547
**Plan:** docs/plans/2026-05-05-omnimarket-public-shippable.md (Task 1)

## Scope

AST scan of `omnimarket/src/omnimarket/**/*.py` for the three call patterns the
plan defines as "raw env-var usage":

- `os.environ.get(KEY[, default])`
- `os.environ[KEY]`
- `os.getenv(KEY[, default])`

Other env-related expressions (`os.environ.copy()`, `os.environ.pop()`,
`os.environ.setdefault()`, `dict(os.environ)`, `os.environ.items()`) are
intentionally out of scope: those mutate the environment dict or take a
process-wide snapshot, they don't read a single declared key. Settings replaces
keyed reads.

## Output

- **Audit script:** `scripts/audit/raw_env_usage_audit.py`
- **CSV:** `docs/audits/2026-05-05-raw-env-usage.csv`
- **Row count:** 259
- **Cross-check:** `rg -nP 'os\.environ\.get|os\.getenv|os\.environ\['
  src/omnimarket/ | wc -l` returns 268. The 9-row delta comes from multiple
  matches on a single line (e.g. coalesced `os.environ.get(A) or
  os.environ.get(B)`) and from multi-line argument splits — both collapse to
  one AST `Call` node per logical call.

## Schema

| Column          | Meaning                                                                |
|-----------------|------------------------------------------------------------------------|
| `file`          | repo-relative path                                                     |
| `line`          | 1-indexed line of the call                                             |
| `kind`          | `os.environ.get` / `os.environ[]` / `os.getenv`                        |
| `key`           | string literal of the key when statically resolvable, else `<expr>`    |
| `has_default`   | `true` if a default was supplied                                       |
| `default_value` | repr of the default (if literal) or unparsed expression                |
| `context`       | source line at `file:line`, stripped                                   |

## Use

This CSV is the canonical scope for:

- **Task 2** (Settings field set): every `key` value in this CSV is a candidate
  field on `omnimarket.config.Settings`.
- **Task 3** (strip baked defaults): every row with `has_default == true` and a
  default that contains `192.168.`, `/Users/`, `/Volumes/`, `cyankiwi/`,
  `Corianas/`, `mlx-community/`, `dash.dev.omninode.ai`, `jonahgabriel`, or a
  similar identity-leak literal is a Task 3 replacement target. Other defaults
  are evaluated case-by-case (some are legitimate empty strings, ints, or
  policy-neutral fallbacks like `"0"`).
