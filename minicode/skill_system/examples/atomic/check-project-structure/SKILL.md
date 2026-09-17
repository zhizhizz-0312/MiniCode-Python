---
name: check-project-structure
version: 1.0.0
description: Verify the project layout, packaging metadata and test entry points exist
triggers: [项目结构, structure, 布局, packaging, 打包配置]
tools: [list_files, read_file, grep_files, run_command]
risk_level: low
kind: atomic
---

# Check project structure

Confirm the repository is shaped like a releasable Python project before anything
irreversible touches it.

## When to use

- A release or packaging step is about to start.
- A build fails and the cause may be a missing file rather than broken code.
- A repository is being onboarded and its layout is still unknown.

## Procedure

1. List the repository root and record whether `pyproject.toml`, `setup.py` or `setup.cfg` exists.
2. Read the packaging file and confirm `name`, `version` and `requires-python` are declared.
3. Confirm the declared package directory exists and contains an `__init__.py`.
4. Confirm the test directory exists and holds at least one `test_*.py` file.
5. Report every missing or mismatched item as a checklist entry with its expected path.

## Guardrails

- Read only; never create the missing files as part of this check.
- A missing `pyproject.toml` in a library is blocking, not advisory.
- Report relative paths, never absolute ones, so the output stays portable.
