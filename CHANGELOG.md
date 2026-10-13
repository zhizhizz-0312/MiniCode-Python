# Changelog

Notable changes in this fork.  Dates are when the work landed locally; nothing here has
been pushed upstream.

Versioning note: the fork starts from upstream's `0.1.0` and is published as `0.2.0`.
It deliberately does **not** claim `1.0`, because the multi-agent cost accounting is still
incomplete (see P4) and calling that finished would overstate what has been verified.

## 0.2.0 — 2026-09-18

### P4 — Multi-agent orchestration (`minicode/orchestration/`)

Plans are composite skills, so a human reads a plan before anything runs.  Added:

- **Plan model and hard budgets** (`plan`, `budget`) — the DAG and its condition semantics
  come from P1's own parser; only what the DAG cannot express lives in a sibling
  `orchestration.json`.  Budgets are two-layer, checked *before* each step, and exhaustion
  yields a status with partial work rather than an exception.
- **git worktree isolation** (`worktree`) — refuses a dirty tree or a non-repository with
  git's own reason listed, records the base commit, and returns each writer's work as a
  patch plus a branch that is never merged automatically.  The upstream `WorktreeIsolator`
  is deliberately not used: it passes contradictory flags and ignores git's exit status,
  so a failed isolation looked like success.
- **Bounded execution agents** (`worker`) — depth is fixed at one structurally (a filtered
  tool registry, and a whitelist naming a spawning tool is refused), the whitelist *is* the
  registry, and a writer that cannot be isolated is reported failed rather than quietly run
  in the main tree.
- **Scheduler and aggregation** (`orchestrator`) — readiness via P1's `ready_steps`,
  bounded concurrency, dependency failures producing `skipped` (never `done`), verification
  run inside the worktree that made the change, and unverified work listed separately from
  proven work.
- **One attributed audit trail** (`audit`) with replay that cross-checks the two ways an
  audit chain lies: a child that never reported, and a run total that does not equal the
  sum of its parts.  Security decisions keep P2's `ruleId` and are mirrored into P2's trail.
- **Command line and commands** — `python -m minicode.orchestration plan|run|agents|replay`,
  `/orchestration`, and `/skills run <plan>`.
- **Local web panel** (`minicode/web_panel/`) — standard library only, bound to 127.0.0.1,
  token-gated with an `Origin` check, read-only by default, and its only two write actions
  go through P3's review gate and P1's lifecycle rather than adding new write paths.

Measured (`benchmarks/orchestration_eval.py`, three read-only subtasks, same plan, only the
concurrency changed): sequential 6.3s, parallel **1.9s**, both 3/3 correct — a **3.26x
speedup** against a 1.5x gate.  The cost half of that gate is not evaluable yet, because
the turn runner reports no usage; the script prints *unknown* rather than zero.

### P3 — Self-learning loop (`minicode/distillation/`)

- Mining clusters similar attempts and keeps only those with a lesson: two attempts, one
  success, a correction or failure, and a succeeding most-recent attempt.
- Offline distillation with a budget, validated output (the generated skill is re-parsed by
  the registry that will load it), and confidence weighted towards the evidence rather than
  the model's self-assessment.
- A draft area plus human review, where refusals are remembered by content fingerprint and
  every draft is screened before approval **even with the security chain switched off**.
- Measured on the 10-scenario corpus: mean tool calls fall **51%** (median 51.5%); a real
  run produced 9 drafts from 10 clusters in 23s for $0.002.

### P2 — Security review (`minicode/security_chain/`)

- Two boundaries (content entering the context, actions before execution) with a taint
  registry carrying provenance between them, and four layers: deterministic rules,
  target/resource preflight, a dual-channel classifier, and a human gate.
- Fusion is asymmetric: both channels must agree on "low" to allow, any disagreement
  escalates and is recorded, and an unparsable model reply counts as unknown, never low.
- Measured over 61 attacks and 42 benign cases: **100% interception / 7.1% false positives** (with the model channel enabled -- the rule channel on its own intercepts about a third, which is what a free run reports)
  in the default lane, 31% / 2.4% with the model disabled, and `strict` measured *worse*
  (same interception, 14.3% false positives).

### P1 — Skill system (`minicode/skill_system/`)

- Three skill layers, a standard-library frontmatter parser, Chinese-aware trigger matching
  with filler filtering, composites compiled onto `task_graph`, on-demand domain packs, and
  usage statistics with retire candidates.
- A trajectory collector, plus the fixes it needed: `/skills` subcommands made
  discoverable, submit-able and honestly matched, and a bracketed-paste crash fixed.

### Fixes that came out of building the above

- Upstream slash commands that were registered but never dispatched are now implemented.
- Test runs no longer write into the developer's real home: trajectories, skill state and
  orchestration trails are all redirected, and the polluted directories were removed.
- A failed runtime-config load is no longer reported as "ready".

### P4 follow-up, from running it for real

- **One price source for both accounts.**  The adapter's own total was computed from a
  per-model USD table while the report used the published CNY rates, so the same call had two
  answers in two currencies.  `calculate_cost_for` now answers in whichever table matches,
  `add_cost` records the unit, and `AppState.cost_currency` names it.  A live check shows the
  internal account reading CNY after a real call.
- **The panel drained its request body.**  Two tests failed intermittently with
  `ConnectionAbortedError [WinError 10053]`; the handler answered 401 without reading the POST
  body, so the client saw its connection reset.  Every response now drains first, bounded.


- **The figure carries its currency.**  `pricing_for(model)` returns the rates *and* the
  unit, and that unit now travels worker -> outcome -> child result -> page, so a
  dollar-priced model can no longer have its number relabelled as yuan.  The switch to the
  shared lookup left the measured amount unchanged (~CNY 0.0003448 on the same subtask),
  which is the check that the two copies of the published rates agreed before the merge.


- **Cost is priced from the token split.**  The adapter computed the split and a USD cost
  and then published only the context total; a price table charges the bands two hundred to
  one apart, so a figure from the total could only be a floor.  The split now reaches the
  store, and the same subtask moved from `~¥0.000265` to `~¥0.0003448` -- the difference is
  the output tokens, which cost four times fresh input.


The orchestration was exercised by hand against a live model, which found more than the
offline suite could:

- **A writer is always isolated**, whatever the concurrency.  The same plan used to write
  straight into the working tree at `maxConcurrent: 1` and return a patch at `3`, so whether
  a human reviewed an agent's changes depended on a tuning knob.
- **Silent gaps closed.**  With the first writer refused, subtasks beyond the first blocked
  ring vanished from the result entirely.  The blocked sweep now iterates to a fixed point,
  and aggregation gives any unresolved subtask a terminal status -- the promise has to be
  structural, because the path that produced the gap is the one most likely to change.
- **Isolated writers can see the project's skills.**  `git worktree add` carries tracked
  content only, and this project keeps skills under a gitignored `.mini-code/`, so every
  writer sandbox failed its `load_skill` calls.  The mirror fixes the child's view and is
  excluded from its diff.
- **Model output can no longer steer the report's rendering.**  A child's summary containing
  a markdown fence flipped the terminal into code-block mode and swallowed the next
  command's output.
- **A plan referencing an uninstalled skill is refused at load time.**  The validator's
  allow-list was built from the referenced names themselves, so that check could never fire.
- **Real token usage is collected**, and priced with the published CNY table rather than
  upstream's flat per-token constant.  The data was always there -- the adapter parses it
  and publishes it through the store it is handed; handed none, it dropped it.

### Known limitations

See the README's *Known limitations*: incomplete cost accounting, a synthetic learning
corpus, a non-interactive false-positive measurement, two environment-dependent test
failures, and a hand-written (dependency-free) web panel.
