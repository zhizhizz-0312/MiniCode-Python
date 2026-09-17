---
name: build-artifacts
version: 1.0.0
description: Build the wheel and sdist from the tagged commit and list the produced files
triggers: [构建产物, build, 打包, wheel, sdist]
tools: [run_command, list_files, read_file]
risk_level: medium
kind: atomic
---

# Build artifacts

Produce the distributable wheel and sdist for exactly one tagged commit.

## When to use

- A tag exists and its artifacts must be built for distribution.
- A build must be reproduced to compare against a previous release.
- Packaging metadata changed and the build output must be re-checked.

## Procedure

1. Confirm the working tree is at the tagged commit and clean.
2. Remove any stale build directory so old artifacts cannot be mistaken for new ones.
3. Run the project build frontend from the repository root.
4. List the produced files with their sizes and record the artifact names.
5. Sanity-check that the wheel contains the expected package files and metadata.

## Guardrails

- Never build from a dirty tree or from an untagged commit for a release.
- Never upload the artifacts; publishing is a separate, confirmed action.
- Report a build warning that affects the package contents instead of ignoring it.
