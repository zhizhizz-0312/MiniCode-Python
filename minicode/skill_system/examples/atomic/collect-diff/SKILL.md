---
name: collect-diff
version: 1.0.0
description: Collect the pending diff against the base ref with file and line statistics
triggers: [收集diff, collect diff, 改动, git diff, 变更清单]
tools: [run_command, read_file, list_files]
risk_level: low
kind: atomic
---

# Collect diff

Assemble one reviewable diff plus the facts a reviewer needs to read it.

## When to use

- A review, audit or commit message needs the exact set of pending changes.
- The base ref is unclear and the comparison target must be pinned first.
- A large change must be split into per-file hunks before review.

## Procedure

1. Determine the base ref: the merge base with the main branch unless told otherwise.
2. Collect the branch, staged and unstaged diffs into one ordered change set.
3. Record per-file statistics and mark files as added, modified, deleted or renamed.
4. Flag binary files, mode changes and dependency lockfile churn separately.
5. Truncate very large hunks with an explicit marker instead of silently dropping them.
6. Report the base ref, the file list and the total line counts.

## Guardrails

- Never modify the working tree, index or stash to produce a cleaner diff.
- Never run a checkpoint or commit as part of collection.
- Report generated and vendored files honestly; do not hide them from the file list.
