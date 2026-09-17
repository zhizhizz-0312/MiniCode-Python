---
name: release-flow
version: 1.0.0
description: Orchestrate a verified release from layout check to tag, build and rollback
triggers: [发版流程, release, 发布, 打tag, 上线]
tools: [load_skill, run_command, read_file, write_file, ask_user]
risk_level: high
kind: composite
---

# Release flow

Cut one release with a hard verification gate in front of every irreversible step.

## When to use

- A maintainer asks to release, publish or ship a new version.
- A tag must be created and build artifacts produced from that exact commit.
- A previous release attempt aborted and the tree must be rolled back first.

## Steps

1. structure -> check-project-structure.
2. tests -> run-test-suite, after structure.
3. version -> bump-version, after tests.
4. tag -> tag-release, after version.
5. build -> build-artifacts, after tag.
6. rollback -> rollback-release, only when tag fails.

## Procedure

1. Load each referenced atomic skill and read its guardrails before running anything.
2. Run the steps in declared order; stop the chain as soon as a gate fails.
3. On success report the tag name plus artifact paths; on abort report the rollback command.

## Guardrails

- Never move or force-push a tag that already exists on the remote.
- Never bump a version while the test suite is red.
- Publishing to an index is out of scope; the flow stops at local artifacts.
