---
name: check-dependencies
version: 1.0.0
description: Verify installed distributions and imports match the declared requirements
triggers: [依赖冲突, dependencies, 依赖检查, 缺包, import error]
tools: [run_command, read_file, grep_files]
risk_level: low
kind: atomic
---

# Check dependencies

Prove whether the installed dependency set can satisfy the declared requirements.

## When to use

- An import fails and the package may be missing or shadowed.
- Two requirements constrain the same package incompatibly.
- Code works locally but not in a clean environment.

## Procedure

1. Read the declared dependency set from the project metadata.
2. List installed distributions with versions for the active interpreter.
3. Diff declared against installed and report missing, extra and version-mismatched entries.
4. For every top-level import used by the project, confirm it resolves and record its file path.
5. Check for shadowing: a local module whose name collides with a dependency.
6. Report the conflicting pairs with the versions each requirement demands.

## Guardrails

- Never install, upgrade or uninstall a package as part of the check.
- Never edit a lockfile to silence a reported conflict.
- Report an unsatisfiable requirement as such instead of picking a winner silently.
