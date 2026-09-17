---
name: bump-version
version: 1.0.0
description: Update every file that declares the project version and verify none is stale
triggers: [改版本号, bump version, 版本升级, semver, 版本号]
tools: [read_file, grep_files, edit_file, run_command]
risk_level: medium
kind: atomic
---

# Bump version

Move the project from one version string to the next without leaving a stale copy.

## When to use

- A release needs the new version written into the repository.
- Two files disagree about the current version and one must win.
- A version string must move without re-tagging anything.

## Procedure

1. Grep the tree for the current version string to find every declaring file.
2. Classify each hit: packaging metadata, package `__init__`, docs, lockfiles.
3. Decide the next version from the requested bump kind, defaulting to patch.
4. Edit each declaring file, keeping formatting and quoting untouched.
5. Re-grep for the old string and confirm only intentional legacy references remain.
6. Report the old version, the new version and every file changed.

## Guardrails

- Never edit lockfiles by hand; regenerate them with the project tool instead.
- Never bump a major version without explicit human confirmation.
- Do not commit or tag here; this skill only rewrites the version.
