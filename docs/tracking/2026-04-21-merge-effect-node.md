# Merge Effect Node (OMN-9404 Pick #5)

## Overview
Node: `node_merge_effect`
Purpose: Simple git merge resolver for DIRTY PRs - mechanical automation of conflict resolution.

## Architecture
- **Type**: Effect node (`node_type: effect`)
- **Purity**: Impure (produces side-effects via git operations)
- **Idempotent**: Yes (safe to re-run)
- **Timeout**: 60 seconds

## Input Schema

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `repo_path` | `str` | required | Path to the repository |
| `branch` | `str` | required | Branch name to merge into |
| `base_branch` | `str` | `origin/main` | Base branch to merge from |
| `dry_run` | `bool` | `false` | Test merge without committing |

## Output Schema
| Field | Type | Description |
|-------|------|-------------|
| `merged` | `bool` | Whether merge succeeded |
| `conflicts_resolved` | `bool` | Whether conflicts were auto-resolved |
| `requires_llm` | `bool` | Whether LLM-based resolution is needed |
| `error` | `str` | Error message if merge failed |

## Event Flow
- **Subscribes to**: `onex.cmd.omnimarket.pr-merge.v1`
- **Publishes to**: `onex.evt.omnimarket.merge-result.v1`
- **Terminal event**: `onex.evt.omnimarket.merge-complete.v1`

## Integration Notes
- **Dependencies**: None (uses subprocess for git)
- **Entry point**: `omnimarket.nodes.node_merge_effect`
- **Runtime**: Compatible with RuntimeLocal and full runtime

## Error Handling
- Non-existent repo: Returns error in response
- Merge conflict: Returns `requires_llm=True` to trigger LLM resolution
- Git failures: Captured in `error` field

## Pattern Reference
Based on OMN-9404 picks - mechanical automation of PRs #1354/#1356 resolutions.
Simpler than `node_conflict_hunk_effect` (no LLM, just git merge).
