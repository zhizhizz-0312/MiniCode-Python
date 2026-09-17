---
name: rollback-release
version: 1.0.0
description: Undo a failed release by restoring version files and deleting the local tag
triggers: [回滚, rollback, 撤销发版, 恢复版本, release rollback]
tools: [run_command, read_file, edit_file, ask_user]
risk_level: high
kind: atomic
---

# Rollback release

Return the repository to its pre-release state after a release step failed.

## When to use

- Tagging or building failed and the release attempt must be undone.
- A version bump landed but the release was abandoned.
- A mistaken local tag must be removed before re-cutting.

## Procedure

1. Establish what actually happened: which version files changed and which tags exist.
2. Restore the version files to their pre-bump values.
3. Delete the local tag only if it was created by the failed attempt and is not on the remote.
4. Re-check that the working tree matches the last known good commit content.
5. Report exactly what was reverted, what was left alone and why.

## Guardrails

- Never delete or rewrite a tag that exists on the remote; escalate to a human.
- Never use a hard reset to discard unrelated work in progress.
- If any step of the undo is uncertain, stop and report rather than guessing.
