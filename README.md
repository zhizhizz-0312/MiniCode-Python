# MiniCode Python — 二次改造版

<p align="center">
  <strong>A local coding agent, plus four stages of engineering work: skills, security review, a learning loop, and multi-agent orchestration.</strong>
</p>

<p align="center">
  <a href="https://github.com/LiuMengxuan04/MiniCode">MiniCode Main Repo</a>
  |
  <a href="https://github.com/QUSETIONS/MiniCode-Python">Upstream Python Repo</a>
  |
  <a href="README.zh-CN.md">Upstream Chinese readme</a>
</p>

> **This is a secondary-development fork of [QUSETIONS/MiniCode-Python](https://github.com/QUSETIONS/MiniCode-Python).**
> The original project, its design and all of its baseline capability belong to the
> upstream authors; this branch only adds to it, and keeps the upstream layout and the
> bilingual readme in place.  The fork ships **no license file of its own** — the upstream
> project's terms continue to apply, so check them before redistributing.

---

## 改造概要（中文速览）

在保留上游全部能力的前提下追加四个阶段，每阶段都能**独立验证**：

| 阶段 | 做了什么 | 一句话验证 |
| --- | --- | --- |
| **P1 技能体系** | 三层技能（原子 / 组合 / 领域包）、前置元数据解析、中英触发词匹配、组合技能编译到 `task_graph`、轨迹采集 | `/skills`、`/skills doctor`、`/skills plan <组合技能>` |
| **P2 安全审查** | 入口侧（内容进入上下文）+ 出口侧（动作执行前）双边界、taint 来源标记、L1 规则 → L2 预检 → L3 双通道分类 → L4 人工确认 | `python benchmarks/injection_eval.py --mode standard --model` |
| **P3 自学习闭环** | 轨迹 → 候选挖掘（先分组再蒸馏）→ 离线蒸馏 → 草稿区 → **人工确认入库** → 统计反哺 | `python -m minicode.distillation report`、`benchmarks/p3_conversation_test.ps1` |
| **P4 多 Agent 编排** | 计划驱动编排、两层硬预算、git worktree 隔离、统一审计链与回放；本地面板 | `python benchmarks/orchestration_eval.py`、`/orchestration` |

**实测**：注入攻击 61 条 **100% 拦截 / 7.1% 误报**（默认档）；自学习使同类任务工具调用**平均下降 51%**；三个可并行任务**快 3.26×**（同一计划、只改并发度）。
**诚实边界**：成本核算尚未接通（回合运行器不返回用量），真实轨迹仍只有个位数，因此"贵 ≤2×"这半条门槛**无法评估**——已写在下方 Known limitations。

详细的阶段实录（设计判断、被实测推翻的方案、边界说明）见中文项目文档：`MiniCode-Python二次改造项目文档.md`。

---

## What this fork adds

Four stages, each with a command that proves it.  The numbers are measured, and the
limitations are stated in the same place as the features rather than left to be
discovered.

### P1 — Skill system (`minicode/skill_system/`)

Three layers: atomic skills (a `SKILL.md`), composite skills (a `workflow.json` DAG
compiled onto the project's own `task_graph`), and domain packs injected on demand.
Frontmatter is parsed with a standard-library YAML subset, trigger matching handles
Chinese as well as English, and a trajectory collector records every turn for the
learning stage.

### P2 — Security review (`minicode/security_chain/`)

Prompt injection has **two** boundaries, not one.  Content is judged as it enters the
context and marked; actions are judged before they execute; and a taint registry carries
provenance between the two, so an action whose arguments came from untrusted content is
escalated regardless of how harmless it looks on its own.

Measured over 61 attacks and 42 benign cases:

| lane | interception | false positives | latency (p95) |
| --- | --- | --- | --- |
| `heuristic` (no model, free) | 31% | **2.4%** | ~0 ms |
| **`standard` (default)** | **100%** | **7.1%** | 1047 ms |
| `strict` (model on every action) | 100% | 14.3% | 1297 ms |

Two of those rows changed the design rather than confirming it.  Gating the model behind
"the features look suspicious" scored 38% interception — a novel injection matches
nothing anyone wrote down, so the generalising channel must not sit behind the memorising
one.  And `strict` turned out to be *worse*: it converts more benign actions into prompts
without intercepting anything extra.

```bash
python benchmarks/injection_eval.py                            # free: the heuristic lane
python benchmarks/injection_eval.py --mode standard --model    # the measured table
```

### P3 — Self-learning loop (`minicode/distillation/`)

Signals (explicit corrections, failures, tool errors) → mining (cluster similar attempts,
keep only those with a lesson) → offline distillation → **draft area → human review** →
the P1 registry.

What it refuses is most of the design: a task seen once is never distilled, nothing
installs itself, a rejected proposal is remembered by content fingerprint, and a draft is
screened before approval **even when `MINI_CODE_SECURITY_MODE=off`** — an injection can
mislead an agent once, but distilled into a skill it becomes standing policy.

Measured over the 10-scenario corpus: mean tool calls fall **51%** (median 51.5%), and a
real distillation run produced 9 drafts from 10 clusters in 23s for $0.002.

```bash
python -m minicode.distillation report                             # corpus, gate, queue, spend (free)
powershell -NoProfile -File benchmarks/p3_conversation_test.ps1     # end to end, 14 assertions
```

### P4 — Multi-agent orchestration (`minicode/orchestration/`)

A plan is a composite skill, so a human reads it before anything runs.  Execution agents
get a skill whitelist, a tool whitelist and a two-layer budget (per child and per run);
writers work in their own git worktree; parent and children write one attributed audit
trail that replays with cross-checks.

Measured on three read-only subtasks, the same plan run twice with only the concurrency
changed:

| mode | wall clock | correct | status |
| --- | --- | --- | --- |
| sequential | 6.3s | 3/3 | complete |
| parallel | **1.9s** | 3/3 | complete |

**Speedup 3.26x** (gate: ≥1.5x).  The money half of the gate cannot be evaluated yet —
see limitations.

```bash
python benchmarks/orchestration_eval.py     # the table above, for real
python -m minicode.orchestration --help     # plan / run / agents / replay
python -m minicode.web_panel                # local panel, 127.0.0.1 only
```

In a session: `/orchestration plan|run|agents|replay`, `/skills run <plan>`, `/security`,
`/skills drafts`.

---

## Differences from upstream, in one table

| | upstream | this fork |
| --- | --- | --- |
| skills | `SKILL.md` loading | + composites compiled to `task_graph`, domain packs, usage statistics, lifecycle |
| safety | permission prompts for dangerous commands | + two-boundary injection review with taint provenance and an audit trail |
| learning | reflection write-back into memory | + distillation into reviewable skill drafts, with a human gate |
| parallelism | background tasks | + plan-driven orchestration with budgets, worktree isolation and replay |
| observability | CLI output | + `/security`, `/orchestration`, a local web panel |
| dependencies | none | **still none** — every addition is standard library |

## Known limitations

* **Cost accounting is incomplete.**  The turn runner does not report token or cost usage,
  so the per-child and per-run cost caps cannot bind on real money yet, and the
  orchestration evaluation prints its cost columns as *unknown* rather than zero.
* **The learning payoff is measured on a synthetic corpus.**  Real trajectories number in
  the single digits, so `/skills distill` on real data honestly reports that no candidate
  passed the gate.
* **The security false-positive figure is measured non-interactively**, where an
  escalation resolves to a denial because nobody can be asked.  In an interactive session
  those cases are prompts, and a prompt the user allows is a nuisance rather than a wrong
  answer.
* **Two tests fail in this environment**: one is order-dependent, the other skips where
  symlinks cannot be created.  Full suite: 1870 passed, 3 skipped.
* **The web panel is a hand-written page**, not a front-end application: the repository
  has no runtime dependencies, and adding a build step would break that.

## Release gates

The release readiness gates this project defines, run individually:

```bash
python -m minicode.release_readiness --check-fallback-evidence benchmarks/release_readiness_results.json
python -m minicode.release_readiness --check-release-report benchmarks/release_readiness_results.json
python -m minicode.release_readiness --check-release-markdown benchmarks/release_readiness_results.md --release-json benchmarks/release_readiness_results.json
```

## Getting started

```bash
python -m venv .venv             # keep the virtualenv outside the repository
.venv/Scripts/pip install -e .   # Windows; use .venv/bin on POSIX
.venv/Scripts/minicode-py        # the terminal agent
```

## Development

```bash
pytest -q --import-mode=importlib
python -m minicode.structure_check --root . --max-dependency-upstream 4 --check-material-inventory
python -m minicode.readiness --fail-on blocked
```
