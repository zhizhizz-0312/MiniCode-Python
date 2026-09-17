---
name: code-review
version: 1.0.0
description: Collect the pending diff, review it, scan for secrets and gate the merge
triggers: [代码评审, code review, 评审, diff审查, 合并前检查]
tools: [load_skill, run_command, read_file, grep_files, write_file]
risk_level: medium
kind: composite
---

# Code review

Turn a working-tree diff into a defensible merge decision without leaking secrets.

## When to use

- A branch or pull request asks for review before merging.
- A commit is about to be pushed and its diff has never been read closely.
- A reviewer wants a written verdict instead of an inline comment thread.

## Steps

1. diff -> collect-diff.
2. review -> review-diff, after diff.
3. secrets -> scan-secrets, after diff (runs in parallel with review, independently).
4. report -> summarize-review, after review and secrets (always runs, even when a step failed).
5. merge-ready -> confirm-merge-readiness, after report, only when review passed.

## Procedure

1. Load the referenced atomic skills and fix the base ref once, up front.
2. Collect the diff once, then run the review and the secret scan against it in parallel.
3. Summarise both results into one report; the summary always runs, so a failed step still produces a written outcome.
4. Confirm merge readiness only when review succeeded; otherwise the summary is the deliverable.

## Guardrails

- Treat any suspected credential as blocking: rotate before re-review, never paste the value.
- Never edit the reviewed code as part of the review; report findings instead.
- Reviewing a diff is not approval to push, merge or deploy.
