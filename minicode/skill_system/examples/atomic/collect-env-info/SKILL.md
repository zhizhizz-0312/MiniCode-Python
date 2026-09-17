---
name: collect-env-info
version: 1.0.0
description: Collect interpreter path, versions, dependency manifest and key config
triggers: [收集环境, env info, 环境信息, 版本信息, 配置检查]
tools: [run_command, read_file, list_files]
risk_level: low
kind: atomic
---

# Collect environment info

Take one timestamped snapshot of the facts every later check will read.

## When to use

- An environment problem is reported and its facts are not yet known.
- Two machines behave differently and their differences must be compared.
- A diagnosis needs reproducible evidence rather than recollection.

## Procedure

1. Record the interpreter: executable path, `sys.version`, implementation and platform.
2. Record whether a virtualenv is active and where its base prefix points.
3. Capture the declared dependencies from `pyproject.toml` or `requirements*.txt`.
4. Capture the installed distribution list with versions for the active interpreter.
5. Record the key config paths in use: settings file, env file names, and their existence.
6. Emit one snapshot, then hand the identical snapshot to every later check.

## Guardrails

- Read only; never install, upgrade or modify configuration while collecting.
- Redact values of anything that looks like a credential; report key names only.
- Record commands and their raw output so the snapshot can be reproduced.
