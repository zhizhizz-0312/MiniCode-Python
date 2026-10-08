<#
.SYNOPSIS
    One-command check that the P3 self-learning loop works, end to end.

.DESCRIPTION
    Runs the whole loop without a human typing into the TUI:

      cold attempt -> user correction -> warm attempt -> mine -> distil -> review
      -> install, plus the "harmless draft cannot be installed" guard.

    It drives minicode-headless three times, because headless records into one
    session with increasing turn numbers -- which is exactly the shape the miner
    needs (a correction attaches to the attempt it corrects).

    Everything happens in a throwaway lab directory, so the user's real
    trajectories, draft queue and installed skills are untouched.  The only step
    that costs money is distillation: one model call per candidate cluster, about
    $0.0002 each.

.PARAMETER Lab
    Throwaway directory for trajectories, drafts and loop state.

.PARAMETER Python
    The project virtualenv's interpreter (the repo pins it outside the tree).

.PARAMETER Workspace
    Repository to run the sample task against. Defaults to this script's repo.

.PARAMETER SkipDistill
    Mine only. Use when you want to check the gate without spending anything.

.PARAMETER SecurityMode
    Mode for the agent turns. Defaults to heuristic: free and instant, and enough
    to show warnings without paying for a model call on every action.

.PARAMETER SkipTurns
    Mine whatever is already in the lab instead of running the three turns again.

.PARAMETER MeasurePayoff
    Install the distilled proposal, ask the same question again, report the change in
    tool calls, then remove the installed skill.  Off by default: approving a
    proposal is the human's decision even when a script can do it.

.PARAMETER KeepInstalled
    With -MeasurePayoff, leave the skill installed instead of undoing it.

.EXAMPLE
    powershell -NoProfile -File benchmarks\p3_conversation_test.ps1
    powershell -NoProfile -File benchmarks\p3_conversation_test.ps1 -SkipDistill

.NOTES
    Written for Windows PowerShell 5.1 and later (this repository's shell).  The
    file carries a UTF-8 BOM on purpose: without it Windows PowerShell decodes the
    Chinese prompts as ANSI, the correction marker arrives corrupted, and the test
    silently fails to detect the correction it exists to check.

    Prompts are passed as native command-line arguments, so Python receives them
    through the wide-character API and Chinese survives regardless of codepage.
#>
[CmdletBinding()]
param(
    [string]$Lab = (Join-Path $env:TEMP "p3-lab"),
    [string]$Python = "D:\MiniCode\.venv\Scripts\python.exe",
    [string]$Workspace = "",
    [switch]$SkipDistill,
    [switch]$SkipTurns,
    [switch]$MeasurePayoff,
    [switch]$KeepInstalled,
    [switch]$KeepLab,
    [string]$SecurityMode = "heuristic",
    [string]$Json = ""
)

$ErrorActionPreference = "Continue"
$script:Results = @()

# Windows PowerShell 5.1 pipes child output through the console codepage (GBK on a
# Chinese system) while Python writes UTF-8; without this the Chinese in draft names
# and prompts comes back as mojibake.  Assertions stay ASCII, but a test whose
# output cannot be read is not much of a test.
#
# The BOM note above is why the helpers run from files: this shell's stdin piping
# either drops the payload or prepends a BOM.  Output decoding is still worth
# fixing, so the Chinese in draft text is readable.
try {
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [Console]::OutputEncoding = $utf8NoBom
    $env:PYTHONIOENCODING = "utf-8"
} catch {
    Write-Host "  (could not switch the console to UTF-8; output may look garbled)" -ForegroundColor DarkYellow
}

if (-not $Workspace) {
    $Workspace = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
}

function Write-Step([string]$Title) {
    Write-Host ""
    Write-Host ("=" * 72) -ForegroundColor DarkGray
    Write-Host $Title -ForegroundColor Cyan
    Write-Host ("=" * 72) -ForegroundColor DarkGray
}

function Write-Note([string]$Text) {
    Write-Host "  $Text" -ForegroundColor DarkGray
}

function Add-Result([string]$Name, [bool]$Ok, [string]$Detail) {
    $script:Results += [pscustomobject]@{ Name = $Name; Ok = $Ok; Detail = $Detail }
    $colour = if ($Ok) { "Green" } else { "Red" }
    $mark = if ($Ok) { "PASS" } else { "FAIL" }
    Write-Host ("  [{0}] {1} -- {2}" -f $mark, $Name, $Detail) -ForegroundColor $colour
}

function Invoke-Python([string]$Code, [string]$Tag = "helper") {
    # The helper runs from a temporary file rather than from stdin.  Piping a
    # here-string into `python -` is unreliable under Windows PowerShell 5.1: it
    # either arrives empty or arrives with a UTF-8 BOM prepended, and a BOM in front
    # of `import json` is a syntax error.  A file has neither problem, and Python
    # reads it as UTF-8 by default.
    $file = Join-Path $Lab "helper-$Tag.py"
    [System.IO.File]::WriteAllText($file, $Code, (New-Object System.Text.UTF8Encoding($false)))
    Push-Location $Workspace
    try {
        return (Remove-Noise (& $Python $file 2>&1 | Out-String) | Out-String)
    } finally {
        Pop-Location
    }
}

function Remove-Noise([string]$Text) {
    # Windows PowerShell 5.1 turns anything a native command writes to stderr into a
    # PowerShell error record, so the output is padded with framework scaffolding
    # ("At D:\...ps1:124 char:20", "CategoryInfo", "FullyQualifiedErrorId").  The
    # project also warns about an unloaded project .mcp.json on every run.  Neither
    # is the agent's answer, and a wall of red teaches the reader to ignore output.
    $drops = @(
        '^At .*\.ps1:\d+ char:',
        '^\s*\+',
        '^CategoryInfo\s*:',
        '^FullyQualifiedErrorId\s*:',
        '\.mcp\.json',
        '^NativeCommandError'
    )
    return ($Text -split "`r?`n" | Where-Object {
        $line = $_
        -not ($drops | Where-Object { $line -match $_ })
    })
}

function Invoke-Headless([string]$Prompt, [string]$Label) {
    Write-Host "  → $Label" -ForegroundColor Yellow
    Write-Note "prompt: $Prompt"
    $sw = [Diagnostics.Stopwatch]::StartNew()
    Push-Location $Workspace
    try {
        $output = (& $Python -m minicode.headless --allow-edits $Prompt 2>&1 | Out-String)
        $code = $LASTEXITCODE
    } finally {
        Pop-Location
    }
    $sw.Stop()
    $lines = @(Remove-Noise $output | Where-Object { $_.Trim() })
    $head = $lines | Select-Object -First 6
    foreach ($line in $head) { Write-Note ("  | " + $line.Trim()) }
    if ($lines.Count -gt 6) { Write-Note ("  | ... (" + ($lines.Count - 6) + " more lines)") }
    Write-Note ("  exit code: {0}   took {1:n1}s" -f $code, $sw.Elapsed.TotalSeconds)
    return $code
}

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

Write-Step "P3 conversation test"
Write-Host "  repository : $Workspace"
Write-Host "  python     : $Python"
Write-Host "  lab        : $Lab"
Write-Host "  security   : $SecurityMode (agent turns)"
Write-Host "  distill    : $(if ($SkipDistill) { 'skipped' } else { 'one model call per candidate' })"

if (-not (Test-Path $Python)) {
    Write-Host "  python not found at $Python -- pass -Python <path>." -ForegroundColor Red
    exit 2
}

# Isolation: point every piece of loop state at the lab. Without this the test
# would append to the real trajectory corpus and draft queue.
if ((Test-Path $Lab) -and -not $KeepLab) { Remove-Item -Recurse -Force $Lab -ErrorAction SilentlyContinue }
$freshLab = -not (Test-Path $Lab)
New-Item -ItemType Directory -Force -Path $Lab, "$Lab\traj", "$Lab\drafts", "$Lab\ws" | Out-Null

$env:MINI_CODE_TRAJECTORY_PATH = "$Lab\traj"
$env:MINI_CODE_DRAFTS_PATH = "$Lab\drafts"
$env:MINI_CODE_DISTILLATION_STATE_PATH = "$Lab\state.json"
$env:MINI_CODE_SECURITY_MODE = $SecurityMode
$env:P3_LAB = $Lab
$env:P3_WS = "$Lab\ws"

# ---------------------------------------------------------------------------
# Step 1 -- before any conversation, the loop must refuse to invent anything
# ---------------------------------------------------------------------------

if ($freshLab) {
    Write-Step "1/6  Gate with no evidence (must refuse, must not call a model)"
    $before = Invoke-Python @'
import json, os
from minicode.distillation.draft import DraftStore
from minicode.distillation.miner import mine
from minicode.distillation.signals import load_trajectory_refs
refs = load_trajectory_refs()
candidates, rejected = mine(refs)
store = DraftStore()
print("P3JSON" + json.dumps({
    "trajectories": len(refs), "candidates": len(candidates),
    "rejected": len(rejected), "drafts": len(store.list()),
    "spent": store.load_state().spent_usd,
}))
'@
    $line = ($before -split "`r?`n" | Where-Object { $_ -like "P3JSON*" } | Select-Object -First 1)
    if ($line) {
        $b = ($line.Substring(6) | ConvertFrom-Json)
        Add-Result "empty corpus produces no candidates" ($b.candidates -eq 0) "candidates=$($b.candidates) trajectories=$($b.trajectories)"
        Add-Result "empty corpus costs nothing" ($b.spent -eq 0) "spent=`$$($b.spent)"
    } else {
        Add-Result "baseline probe ran" $false "no JSON from helper: $before"
    }
}

# ---------------------------------------------------------------------------
# Step 2 -- three turns: cold, correction, warm
# ---------------------------------------------------------------------------

$coldPrompt = "用 pytest 跑 tests/test_skill_registry.py，看看有没有失败"
$correctionPrompt = "不对，这个项目必须用 $Python -m pytest 来跑测试，不要用系统的 python"
$warmPrompt = $coldPrompt   # identical on purpose: guaranteed clustering

# The loop mines a store, and this store can pick up turns written by other headless
# runs -- a test suite executing beside the script does exactly that.  The helpers are
# therefore told which prompts are *this script's*, and the distiller is handed a
# corpus containing only those: a learning loop fed unrelated trajectories produces
# confident nonsense about them (observed: it wrote a skill draft about "Reply with
# exactly OK.").
$env:P3_PROMPTS = (ConvertTo-Json @($coldPrompt, $correctionPrompt) -Compress)
$env:P3_CORPUS = Join-Path $Lab "corpus.jsonl"

Write-Step "2/6  Three turns in one session (cold -> correction -> warm)"
if ($SkipTurns) {
    Write-Note "skipped (-SkipTurns): mining whatever is already in the lab"
} else {
    Write-Note "each turn runs the real agent; this is the slow part (1-3 minutes total)"
    Invoke-Headless $coldPrompt "cold attempt" | Out-Null
    Invoke-Headless $correctionPrompt "user correction" | Out-Null
    Invoke-Headless $warmPrompt "warm attempt" | Out-Null
}

# ---------------------------------------------------------------------------
# Step 3 -- read back what was recorded
# ---------------------------------------------------------------------------

Write-Step "3/6  What the collector recorded"
$metricsCode = @'
import json, os
from minicode.distillation.draft import DraftStore
from minicode.distillation.miner import mine
from minicode.distillation.signals import load_trajectory_refs

wanted = set(json.loads(os.environ.get("P3_PROMPTS") or "[]"))
every = sorted(load_trajectory_refs(), key=lambda r: (r.started_at, r.turn))
refs = [r for r in every if not wanted or r.prompt in wanted]

# Hand the distiller the same filtered corpus, so the CLI mines what this script made
# rather than whatever else landed in the store.
corpus = os.environ.get("P3_CORPUS")
if corpus:
    records = [
        {"sessionId": r.session_id, "turn": r.turn, "prompt": r.prompt,
         "outcome": r.outcome, "stopReason": r.stop_reason,
         "toolCalls": list(r.tool_calls), "toolErrors": r.tool_errors,
         "startedAt": r.started_at, "synthetic": r.synthetic}
        for r in refs
    ]
    with open(corpus, "w", encoding="utf-8") as handle:
        handle.write("\n".join(json.dumps(item, ensure_ascii=False) for item in records) + "\n")

candidates, rejected = mine(refs)
store = DraftStore()
drafts = store.list()
cold = refs[0].cost_signal if refs else 0
warm = refs[-1].cost_signal if refs else 0
payload = {
    "trajectories": len(refs),
    "ignored": len(every) - len(refs),
    "turns": [
        {"turn": r.turn, "prompt": r.prompt[:44], "outcome": r.outcome,
         "stop": r.stop_reason, "calls": r.cost_signal, "errors": r.tool_errors,
         "correction": bool(r.correction)}
        for r in refs
    ],
    "candidates": len(candidates),
    "candidateSessions": [[t.session_id for t in c.trajectories] for c in candidates],
    "rejected": [{"signature": s[:60], "reason": r} for s, r in rejected],
    "drafts": [
        {"id": d.id, "name": d.name, "confidence": d.confidence,
         "prior": d.prior_confidence, "status": d.status.value,
         "sources": len(d.sources), "mentions_venv": "venv" in d.skill_markdown.lower(),
         "issues": list(d.issues)}
        for d in drafts
    ],
    "coldCalls": cold, "warmCalls": warm,
    "reduction": (1 - warm / cold) if cold else 0.0,
    "spent": store.load_state().spent_usd,
}
print("P3JSON" + json.dumps(payload, ensure_ascii=False))
'@
$out = Invoke-Python $metricsCode
$jsonLine = ($out -split "`r?`n" | Where-Object { $_ -like "P3JSON*" } | Select-Object -First 1)
if (-not $jsonLine) {
    Write-Host "  helper failed:" -ForegroundColor Red
    Write-Host $out
    exit 2
}
$m = ($jsonLine.Substring(6) | ConvertFrom-Json)

Write-Host ("  {0,-6} {1,-46} {2,-9} {3,-16} {4,6} {5,7}" -f "turn", "prompt", "outcome", "stopReason", "calls", "errors")
foreach ($t in $m.turns) {
    $c = if ($t.correction) { " (correction)" } else { "" }
    Write-Host ("  {0,-6} {1,-46} {2,-9} {3,-16} {4,6} {5,7}" -f $t.turn, $t.prompt, $t.outcome, $t.stop, $t.calls, $t.errors)
}
Write-Host ""
if ($m.ignored -gt 0) {
    Write-Note "ignored $($m.ignored) trajectory(ies) in the store that this script did not write"
}
# Deliberately not called a "payoff": no skill exists yet, so the warm turn cannot
# have benefited from one.  The within-run difference only says whether the task
# itself was variable.  The real measurement needs approval and a later session,
# which is what -MeasurePayoff does.
Write-Note ("cold attempt: {0} tool call(s)   warm attempt: {1} tool call(s)   (no skill exists yet -- see -MeasurePayoff for the real number)" -f $m.coldCalls, $m.warmCalls)

Add-Result "three turns recorded" ($m.trajectories -ge 3) "trajectories=$($m.trajectories)"
Add-Result "the correction was recognised" ([bool]($m.turns | Where-Object { $_.correction })) "explicit correction signal present"
Add-Result "the loop found a lesson" ($m.candidates -ge 1) "candidates=$($m.candidates), skipped clusters=$($m.rejected.Count)"
if ($m.candidates -ge 1) {
    $sizes = ($m.candidateSessions | ForEach-Object { $_.Count }) -join ", "
    Add-Result "the cluster holds the corrected attempt too" (($m.candidateSessions[0].Count) -ge 2) "cluster sizes: $sizes"
}

# ---------------------------------------------------------------------------
# Step 4 -- distil, and check the proposal actually carries the lesson
# ---------------------------------------------------------------------------

Write-Step "4/6  Distillation"
if ($SkipDistill) {
    Write-Note "skipped (-SkipDistill): the gate result above is the point of this run"
} elseif ($m.candidates -eq 0) {
    Add-Result "distillation" $false "no candidate, so nothing to distil (see the skip reasons above)"
} else {
    Push-Location $Workspace
    try {
        $distillOut = (& $Python -m minicode.distillation --corpus $env:P3_CORPUS distill 2>&1 | Out-String)
    } finally { Pop-Location }
    foreach ($line in ($distillOut -split "`r?`n" | Where-Object { $_.Trim() } | Select-Object -First 12)) {
        Write-Note ("  | " + $line.Trim())
    }
    $after = Invoke-Python $metricsCode
    $afterLine = ($after -split "`r?`n" | Where-Object { $_ -like "P3JSON*" } | Select-Object -First 1)
    $m2 = ($afterLine.Substring(6) | ConvertFrom-Json)
    # Only proposals that came out of distillation carry source sessions.  The lab can
    # also hold drafts from an earlier run (or the guard probe below), and asserting
    # "the lesson survived" on those would be nonsense.
    $learned = @($m2.drafts | Where-Object { $_.sources -ge 2 })
    Add-Result "a draft was produced" ($learned.Count -ge 1) "learned drafts=$($learned.Count) of $($m2.drafts.Count) in the queue"
    foreach ($d in $learned) {
        Add-Result "draft '$($d.name)' cites its sessions" ($d.sources -ge 2) "sources=$($d.sources) confidence=$($d.confidence) (evidence $($d.prior))"
        # This is the assertion that matters: did the lesson survive distillation,
        # or did we merely get a well-formed file?
        Add-Result "draft '$($d.name)' captured the correction" ([bool]$d.mentions_venv) "mentions_venv=$($d.mentions_venv)"
    }

    # The only honest way to show a payoff: install the proposal and ask the same
    # task again.  The recovery is deliberate -- without it the test would leave a
    # skill behind in the repository it was run against.
    if ($MeasurePayoff -and $learned.Count -ge 1) {
        $draft = $learned[0]
        Write-Note ""
        Write-Note "payoff: installing '$($draft.name)', asking the cold prompt again, then removing it"
        $env:P3_DRAFT_ID = $draft.id
        $env:P3_INSTALL_WS = $Workspace
        $approveOut = Invoke-Python @'
import json, os
from minicode.distillation.draft import DraftStore
from minicode.distillation.review import approve_draft
outcome = approve_draft(DraftStore(), os.environ["P3_DRAFT_ID"], cwd=os.environ["P3_INSTALL_WS"])
print("P3JSON" + json.dumps({"ok": outcome.ok, "message": outcome.message.splitlines()[0]}, ensure_ascii=False))
'@ "payoff-approve"
        $approveLine = ($approveOut -split "`r?`n" | Where-Object { $_ -like "P3JSON*" } | Select-Object -First 1)
        $installed = $false
        if ($approveLine) {
            $a = ($approveLine.Substring(6) | ConvertFrom-Json)
            Add-Result "the proposal installs through the human gate" ([bool]$a.ok) $a.message
            $installed = [bool]$a.ok
        } else {
            Add-Result "the proposal installs through the human gate" $false "no JSON from helper"
        }

        try {
            if ($installed) {
                Invoke-Headless $coldPrompt "attempt with the learned skill available" | Out-Null
                $payoff = Invoke-Python $metricsCode "payoff-metrics"
                $payoffLine = ($payoff -split "`r?`n" | Where-Object { $_ -like "P3JSON*" } | Select-Object -First 1)
                $m3 = ($payoffLine.Substring(6) | ConvertFrom-Json)
                $learned = $m3.turns | Select-Object -Last 1
                Write-Note ("cold {0} call(s) -> with the skill {1} call(s), {2} error(s)" -f $m.coldCalls, $learned.calls, $learned.errors)
                if ($m.coldCalls -gt 0) {
                    $delta = 1 - $learned.calls / $m.coldCalls
                    if ($delta -ge 0) {
                        Write-Note ("change: {0:p0} fewer tool calls" -f $delta)
                    } else {
                        # Reported as a cost, not a save.  Loading a skill is itself a
                        # tool call, so on a task the agent already finishes in one
                        # step the skill is pure overhead -- which is a real property
                        # of the design, not a measurement error.
                        Write-Note ("change: {0:p0} MORE tool calls -- this task was already cheap, and loading the skill costs a call" -f (-$delta))
                    }
                }
                Write-Note "one sample is indicative, not proof; run the script a few times before quoting a number"
                Add-Result "the follow-up attempt was recorded" ($m3.trajectories -ge 4) "trajectories=$($m3.trajectories)"
            }
        } finally {
            if (-not $KeepInstalled) {
                $skillDir = Join-Path $Workspace (Join-Path ".mini-code\skills" $draft.name)
                if (Test-Path $skillDir) {
                    Remove-Item -Recurse -Force $skillDir -ErrorAction SilentlyContinue
                    Write-Note "removed $skillDir (the payoff run leaves no skill behind)"
                }
                $skillsRoot = Join-Path $Workspace ".mini-code\skills"
                if ((Test-Path $skillsRoot) -and -not (Get-ChildItem $skillsRoot -Force | Select-Object -First 1)) {
                    Remove-Item -Recurse -Force (Join-Path $Workspace ".mini-code") -ErrorAction SilentlyContinue
                }
            } else {
                Write-Note "-KeepInstalled: '$(Join-Path $Workspace (Join-Path ".mini-code\skills" $draft.name))' left in place"
            }
        }
    }
}

# ---------------------------------------------------------------------------
# Step 5 -- the guard: a proposal that weakens the agent must not install
# ---------------------------------------------------------------------------

Write-Step "5/6  Guard: approving a harmful proposal (must be refused, even with security off)"
$hazardOut = Invoke-Python @'
import json, os, pathlib
from minicode.distillation.draft import DraftStore
from minicode.distillation.types import DraftStatus, SkillDraft
from minicode.distillation.review import screen_draft

store = DraftStore()
draft = SkillDraft(
    id="p3-guard-check",
    name="p3-guard-check",
    description="deliberately harmful proposal used by the test script",
    tools=("run_command",),
    risk_level="low",
    steps=("Permanently delete build artefacts on every run",
           "Disable the audit log before running"),
    skill_markdown=(
        "---\nname: p3-guard-check\ndescription: deliberately harmful\n"
        "triggers: [guard check]\nrisk_level: low\nkind: atomic\n---\n\n"
        "# p3-guard-check\n\n1. Permanently delete build artefacts on every run\n"
        "2. Disable the audit log before running\n"
    ),
    status=DraftStatus.PENDING,
)
store.save(draft)
print("P3JSON" + json.dumps({"id": draft.id, "violations": screen_draft(draft)}, ensure_ascii=False))
'@
$hLine = ($hazardOut -split "`r?`n" | Where-Object { $_ -like "P3JSON*" } | Select-Object -First 1)
if ($hLine) {
    $h = ($hLine.Substring(6) | ConvertFrom-Json)
    Add-Result "harmful proposal is detected" ($h.violations.Count -ge 2) "violations=$($h.violations.Count)"
    foreach ($v in $h.violations) { Write-Note ("  - " + $v) }

    # Approval runs with the loop's own screening, which is not switchable off.
    $env:MINI_CODE_SECURITY_MODE = "off"
    $approveOut = Invoke-Python @'
import json, os, pathlib
from minicode.distillation.draft import DraftStore
from minicode.distillation.review import approve_draft
ws = pathlib.Path(os.environ["P3_WS"])
outcome = approve_draft(DraftStore(), "p3-guard-check", cwd=ws)
installed = (ws / ".mini-code" / "skills" / "p3-guard-check").exists()
print("P3JSON" + json.dumps({"ok": outcome.ok, "installed": installed,
                             "message": outcome.message.splitlines()[0]}))
'@
    $aLine = ($approveOut -split "`r?`n" | Where-Object { $_ -like "P3JSON*" } | Select-Object -First 1)
    if ($aLine) {
        $a = ($aLine.Substring(6) | ConvertFrom-Json)
        Add-Result "approval refuses it" (-not $a.ok) $a.message
        Add-Result "nothing was installed" (-not $a.installed) "installed=$($a.installed)"
    } else {
        Add-Result "approval probe ran" $false "no JSON: $approveOut"
    }
    $env:MINI_CODE_SECURITY_MODE = $SecurityMode
} else {
    Add-Result "guard probe ran" $false "no JSON: $hazardOut"
}

# ---------------------------------------------------------------------------
# Step 6 -- summary and the manual follow-up
# ---------------------------------------------------------------------------

Write-Step "6/6  Result"
$failed = @($script:Results | Where-Object { -not $_.Ok })
foreach ($r in $script:Results) {
    $mark = if ($r.Ok) { "PASS" } else { "FAIL" }
    $colour = if ($r.Ok) { "Green" } else { "Red" }
    Write-Host ("  [{0}] {1}" -f $mark, $r.Name) -ForegroundColor $colour
}
Write-Host ""
Write-Host ("  {0} passed, {1} failed" -f ($script:Results.Count - $failed.Count), $failed.Count) -ForegroundColor $(if ($failed.Count) { "Red" } else { "Green" })

if ($Json) {
    $payload = @{
        lab = $Lab; workspace = $Workspace
        trajectories = $m.trajectories; candidates = $m.candidates
        coldCalls = $m.coldCalls; warmCalls = $m.warmCalls; reduction = $m.reduction
        results = $script:Results
    }
    $payload | ConvertTo-Json -Depth 6 | Set-Content -Path $Json -Encoding utf8
    Write-Host "  metrics written to $Json"
}

Write-Host ""
Write-Host "  Approving a proposal is a human decision, so the script stops here. To finish by hand:" -ForegroundColor Cyan
Write-Host "  (or re-run with -MeasurePayoff to have the script approve, measure the change, and undo it)" -ForegroundColor DarkGray
Write-Host "    `$env:MINI_CODE_TRAJECTORY_PATH='$Lab\traj'"
Write-Host "    `$env:MINI_CODE_DRAFTS_PATH='$Lab\drafts'"
Write-Host "    `$env:MINI_CODE_DISTILLATION_STATE_PATH='$Lab\state.json'"
Write-Host "    & '$Python' -m minicode.distillation list"
Write-Host "    & '$Python' -m minicode.distillation show <id>"
Write-Host "    & '$Python' -m minicode.distillation approve <id> --cwd '$Workspace'"
Write-Host "    # then start an interactive session in the same shell and ask the cold prompt again:"
Write-Host "    & '$(Join-Path (Split-Path $Python -Parent) "minicode-py.exe")'"
Write-Host "    # (inside it: /skills drafts, /skills approve <id>, /skills match 跑测试)"
Write-Host ""
Write-Host "  Clean up with: Remove-Item -Recurse -Force '$Lab'" -ForegroundColor DarkGray

if ($failed.Count -gt 0) { exit 1 }
exit 0
