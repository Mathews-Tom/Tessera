# Execution Prompt — Run One Tessera Work Unit

Paste this prompt into a fresh Codex session at the Tessera repo root.

You are Codex working in `/Users/druk/WorkSpace/AetherForge/Tessera`. Run exactly one work unit from the canonical development plan, end-to-end, in the main session. Do not start a second work unit. The final answer must end with exactly:

```text
To run the next phase, paste this prompt into a fresh session.
```

## Hard Constraints

Do not use the `Agent` tool to delegate implementation work to a sub-agent. Sub-agents can trigger upstream API content filters on file-generation-heavy work. All implementation, commits, push, PR, review handling, merge, cleanup, tagging, and verification must execute in the main session.

`Explore`-style read-only research and `Plan`-style design discussion are allowed only when they produce no commits and do not move implementation out of the main session.

Use `uv`, not `pip`. Detect package manager before any Node work. Use `apply_patch` for manual edits. Do not use destructive git commands. Do not add AI attribution in commits, PR descriptions, code comments, or generated docs.

Branch names, commit subjects, and PR titles must not contain:

- `phase`
- `Phase`
- `P0`, `P1`, `P2`, `P3`, `P4`, `P5`, `P6`, `P7`, `P8`, `P9`, `P10`
- `R1`, `R2`, `R3`, `E1`, `E2`, `M1`, `M2`, `M3`, `M4`, `M5`, `M6`, `H1`, `H2`, `H3`, `H4`, `H5`, `H6`, `H7`, `H8`, `S1`, `S2`, `S3`
- `K1`, `K2`, `K3`, `K4`, `K5`

Use descriptive names tied to deliverables, for example `release/v0-5-rc1`, `bench/sync-load`, `docs/external-validation`, `sync/row-merge`, `context/disk-backed-facets`, `context/source-refs`, `memory/recall-trace`.

## Required Inputs

Read these first:

```bash
if test -f .docs/development-plan; then PLAN_FILE=.docs/development-plan; elif test -f .docs/development-plan.md; then PLAN_FILE=.docs/development-plan.md; else echo "missing development plan" >&2; exit 2; fi
sed -n '1,260p' "$PLAN_FILE"
git status --short
git branch --show-current
git log --oneline -10
git tag --list --sort=-creatordate | head -20
gh pr list --state merged --limit 10 --json number,title,mergedAt,headRefName,mergeCommit
```

Plan path rule: prefer `.docs/development-plan` when present. If the repo later restores `.docs/development-plan.md` and the extensionless file is absent, use `.docs/development-plan.md`.

## Stop Conditions: Pause And Ask

Pause and ask the user before continuing if any condition occurs:

1. Dirty tree on `main` at session start.
2. Current branch is not `main` at session start.
3. Any irreversible action is required:
   - publishing to PyPI, Homebrew, npm, crates.io, Docker registries, package feeds
   - payments, paid APIs, billing changes
   - production infra changes
   - force-push
   - deleting data outside the repo
4. The detected next work unit conflicts with the canonical development plan.
5. The exit criterion fails after 3 fix attempts on the same root cause.
6. A `pr-review` or `code-refiner` CRITICAL/HIGH finding is contested and you believe the finding should not be fixed.
7. A required secret, external credential, human recording, or external user session is needed.
8. `gh` is not authenticated or cannot push/open/merge PRs.

When pausing, report:

- exact blocker
- commands already run
- relevant file paths
- concrete options for the user

Do not continue until the user answers.

## Detection Table

Determine the next work unit from git state. Use the latest semantic tag first. If no relevant tag exists, use latest merged PR titles and the canonical development plan.

| Git State | Next Work Unit | Branch Name | Dev-Plan Section | Tag At End |
| --- | --- | --- | --- | --- |
| No `v0.5.0rc1` tag exists and latest merged PR includes V0.5-P9b/S3/sync CLI or code contains `src/tessera/sync/s3.py` | v0.5 rc release prep | `release/v0-5-rc1` | `R1 — v0.5.0rc1 Release Prep` | `v0.5.0rc1` |
| `v0.5.0rc1` exists, but no committed 50K sync load result/tracking doc exists | sync load validation | `bench/sync-load` | `R2 — v0.5 Dogfood Gates` | No |
| 50K sync load evidence exists, but no 30-day multi-machine dogfood evidence exists | multi-machine sync dogfood evidence | `docs/sync-dogfood` | `R2 — v0.5 Dogfood Gates` | No |
| 30-day sync evidence exists, but no 60-day compiled-notebook dogfood evidence exists | compiled-notebook dogfood evidence | `docs/compiled-dogfood` | `R2 — v0.5 Dogfood Gates` | No |
| v0.5 dogfood gates complete, but cross-platform smoke evidence incomplete | external validation evidence | `docs/external-validation` | `R3 — External Validation / GA Readiness` | No |
| External validation complete, no `v0.5.0` tag exists, and user approval for public release is present | v0.5 GA release prep | `release/v0-5-ga` | `R3 — External Validation / GA Readiness` | `v0.5.0` |
| `v0.5.0` exists and real sync conflict cases are documented | row-merge implementation | `sync/row-merge` | `E1 — V0.5-P9c Multi-Device Row Merge` | No |
| `v0.5.0` exists and repeated temporal recall requests are documented | temporal retrieval implementation | `retrieval/temporal-recall` | `E2 — V0.5-P10 Episodic Temporal Retrieval` | No |
| `v0.5.0` exists, no conditional engineering trigger is present, and no repo-local project-context adapter exists | repo-local project-context adapter | `context/disk-backed-facets` | `K1 — Repo-Local Project Context Adapter` | No |
| Repo-local project-context adapter exists, but no source-reference index exists | source-reference index | `context/source-refs` | `K2 — Source Reference Index` | No |
| Source-reference index exists, but no context integrity check exists | context integrity check | `context/check` | `K3 — Context Integrity Check` | No |
| Context integrity check exists, but no explicit context expansion exists | explicit context expansion | `context/expand` | `K4 — Explicit Context Expansion` | No |
| Explicit context expansion exists, but no opt-in agent hook scaffolding exists | agent hook scaffolding | `context/hooks` | `K5 — Agent Hook Scaffolding` | No |
| Project context layer is complete and no conditional engineering trigger is present | capture suggestion API | `memory/capture-suggestions` | `M1 — Capture Suggestions` | No |
| Capture suggestions complete | recall trace | `memory/recall-trace` | `M2 — Recall Trace` | No |
| Recall trace complete | memory scope policy | `memory/scopes` | `M3 — Scope-Aware Memory Policy` | No |
| Memory scope policy complete | conflict handling | `memory/conflicts` | `M4 — Conflict Handling` | No |
| Conflict handling complete | memory quality controls | `memory/quality-controls` | `M5 — Memory Quality Controls` | No |
| Memory quality controls complete | automatic memory evals | `eval/memory-policy` | `M6 — Automatic Capture / Recall Evaluation` | No |
| Memory evals complete and housekeeping remains | next housekeeping item by order | descriptive `refactor/...`, `sync/...`, or `cli/...` branch | `H1` through `H8`, first incomplete | No |
| Housekeeping complete and v1.0 not started | multi-user design/implementation | `platform/multi-user` | `S1 — v1.0 Multi-User` | No |
| Multi-user complete, GUI trigger exists | optional GUI | `ui/desktop-console` | `S2 — Optional GUI` | No |
| GUI work complete or skipped, hosted sync trigger exists | hosted sync | `sync/hosted-service` | `S3 — Optional Hosted Sync` | No |
| `v1.0.0` tag exists and no incomplete release-spec gate remains | STOP | none | none | No |

If detection returns `STOP`, do no work. Print the repo state and stop.

If detection selects a work unit that needs human/external evidence and that evidence cannot be produced in this session, create only the tracking/documentation work that is possible, then pause and ask before fabricating evidence.

## Workflow For The One Work Unit

### 1. Orient

Run the required input commands. Confirm:

- on `main`
- clean worktree
- `origin/main` reachable
- the canonical development plan exists
- `gh` authenticated

Fetch latest state:

```bash
git fetch --all --tags --prune
git pull --ff-only origin main
```

If this changes the detected next work unit, re-run detection once.

### 2. Select One Work Unit

Use the detection table. State:

- selected work unit
- branch name
- dev-plan section
- expected deliverables
- whether a tag is expected at the end

Then proceed. Do not ask for confirmation unless a stop condition applies.

### 3. Create Branch

Create a local branch from main:

```bash
git switch -c <branch-name>
```

Branch name must pass the forbidden-string scan.

### 4. Partition The Work

Apply sequential thinking in the main session before editing:

- identify deliverables
- group them into 2-5 logical commits where practical
- define validation for each group
- identify files likely to change
- identify data/evidence that cannot be produced locally

Do not expose chain-of-thought. Write a concise implementation outline in the conversation.

### 5. Implement Inline

Implement only the selected work unit. Do not start another section from the canonical development plan.

Use repo patterns. Search before creating files. Re-read files before editing. Use `apply_patch` for manual edits. Keep commits logically separated.

For each logical group:

```bash
git diff --check
uv run ruff check .
uv run ruff format --check .
uv run mypy --strict src/tessera tests
```

Run targeted tests relevant to the group. Run full tests before PR.

### 6. Commit

Make multiple well-organized commits when the work naturally separates. Commit subjects must be conventional, imperative, under 72 chars, and contain no forbidden strings.

Examples:

```bash
git add <explicit paths>
git commit -m "docs: prepare v0.5 rc release notes"
git commit -m "bench: refresh recall regression baselines"
git commit -m "test: cover sync load reporting"
```

Never use:

- `git add .`
- `git add -A`
- AI attribution
- phase numbers in commit subjects

### 7. Validate Before Push

Required validation:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy --strict src/tessera tests
uv run pytest -m "not slow" --cov=tessera --cov-branch --cov-report=term-missing --cov-fail-under=80
bash scripts/no_telemetry_grep.sh
bash scripts/facet_vocabulary_grep.sh
bash scripts/assume_identity_grep.sh
bash scripts/audit_chain_single_writer.sh
uv run python scripts/audit_chain_determinism.py
git diff --check
```

For release prep, also run the benchmarks named in the canonical development plan.

If validation fails, fix and re-run. After 3 failed attempts on the same root cause, pause and ask.

### 8. Push And Open PR

Push:

```bash
git push -u origin <branch-name>
```

Open a PR with a detailed description covering the full branch scope, not only the latest commit:

```bash
gh pr create --base main --head <branch-name> --title "<descriptive title>" --body-file /tmp/tessera-pr-body.md
```

PR title must not include forbidden strings.

PR body must include:

- Summary
- What changed
- Why it matters
- Files/areas touched
- Validation run with exact commands
- Benchmark/result files, if any
- Risk and rollback notes
- Follow-up items, if any
- Tag-at-end expectation, if any

### 9. Run Reviews And Fix Findings

Run `pr-review` and `code-refiner` skills in the main session. Do not use the Agent tool.

Review protocol:

- Treat CRITICAL/HIGH findings as blocking.
- Fix every CRITICAL/HIGH finding with follow-up commits.
- Re-run relevant validation.
- Push follow-up commits.
- If a CRITICAL/HIGH finding is contested, pause and ask with evidence.
- MEDIUM/LOW findings can be deferred only if documented in the PR body or the canonical development plan and they are not release blockers.

After fixes, re-run the review skills once if the changes were substantial.

### 10. Merge

Wait for CI if GitHub Actions are available:

```bash
gh pr checks --watch
```

If CI is unavailable but local validation is complete, state that in the PR and continue only if repository practice allows it.

Merge:

```bash
gh pr merge --squash --delete-branch
git switch main
git pull --ff-only origin main
```

If squash merge would collapse carefully separated commits and this matters for the selected work unit, use the repo's established merge mode. Do not force merge.

### 11. Tag If Required

If the detection table says `Tag At End`, tag after merge on updated `main`.

Before tagging, pause and ask if tagging would publish externally or trigger a release workflow that publishes packages. A local git tag and push is allowed only if no publish workflow will run without explicit approval.

Tag command:

```bash
git tag -a <tag> -m "<tag>"
git push origin <tag>
```

### 12. Cleanup

Clean local stale branches:

```bash
git fetch --all --tags --prune
git branch --merged main
```

Delete the local feature branch if still present:

```bash
git branch -d <branch-name>
```

Do not delete unrelated branches.

### 13. Self-Verification Block

Run and report the important output:

```bash
gh pr view <pr-number> --json number,title,state,mergedAt,mergeCommit,url
git tag --list --sort=-creatordate | head -20
git branch -a
git status --short
git log --oneline -5
```

Scan for forbidden strings in branch, commit subjects, PR title, and tag message:

```bash
git log --format='%s%n%b' -20 | rg -n 'phase|Phase|P[0-9]+|R[0-9]+|E[0-9]+|M[0-9]+|H[0-9]+|S[0-9]+|Co-Authored-By|Generated with|Codex|Claude' || true
gh pr view <pr-number> --json title,body --jq '.title + "\n" + .body' | rg -n 'phase|Phase|P[0-9]+|R[0-9]+|E[0-9]+|M[0-9]+|H[0-9]+|S[0-9]+|Co-Authored-By|Generated with|Codex|Claude' || true
```

If the scan finds a forbidden string introduced by this work, fix it if possible. If it is in immutable merged history, report it explicitly.

### 14. Final Summary And Stop

Final response must include:

- selected work unit
- branch
- PR URL and merge commit
- tag pushed, if any
- commits made before merge
- validation commands run
- CRITICAL/HIGH review findings and how they were resolved
- follow-ups added or remaining
- confirmation that no second work unit was started

End the final response with exactly:

```text
To run the next phase, paste this prompt into a fresh session.
```
