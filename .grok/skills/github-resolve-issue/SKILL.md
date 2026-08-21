---
name: github-resolve-issue
description: >
  Orchestrate resolving open GitHub issues on an issue-linked branch:
  checkout via gh issue develop, fold with /sdd:spec github issue N
  (auto-approve APPLY), isolated /sdd:build --all, isolated /review
  --branch for simplifications, make check, push, open one GitHub
  PR per issue, then wait for merge approval. --all analyzes
  dependencies first and walks issues in that order, one at a
  time. Never merges without an explicit operator choice. Use
  when the user wants to auto-resolve GitHub issues, drain the
  issue backlog, run the SDD issue loop, or runs
  /github-resolve-issue.
argument-hint: "[N | --all] [--no-wait]"
allowed-tools: ask_user_question, read_file, run_terminal_command, spawn_subagent, monitor, todo_write
---

# github-resolve-issue

Operator-facing loop over open GitHub issues. This skill owns
orchestration only. Spec fold, implementation, branch/PR/merge shape,
and acceptance live in the installed sdd plugin skills — load and
follow them; do not restate their recipes. Simplification review
lives in the bundled `/review` skill — same rule.

**Plugin skills (load from disk, do not copy):**

1. Resolve plugin root: first match of
   `$GROK_HOME/installed-plugins/*/skills/spec/SKILL.md`
   (`GROK_HOME` defaults to `~/.grok`).
2. Read `skills/spec/SKILL.md`, `skills/build/SKILL.md`,
   `skills/github/SKILL.md`, `skills/_fragments/ACCEPTANCE-GATE.md`.
3. On every BRANCH / PR / MERGE op, follow the github skill
   (emit `engaged sdd:github — <op>`). Never github LINEAR —
   this loop always BRANCH + PR. One `gh pr create` per issue.

**Bundled review skill (load from disk, do not copy):**
`$GROK_HOME/bundled/skills/review/SKILL.md`

## Arguments

| Arg | Meaning |
|---|---|
| `N` | Resolve issue `N` only |
| `--all` | Analyze eligible issues, print an ordered `#` list, walk it one at a time until empty or a phase fails |
| (empty) | Oldest eligible open issue, one issue. No analysis |
| `--no-wait` | Open the PR, then stop (still never merges) |

`--all` plus `--no-wait` opens one PR per remaining ordered issue
and continues without waiting.

## Context reset

A skill cannot run `/new` or `/clear` without killing the loop.

**Reset = a new `spawn_subagent` (fresh context) per phase.**
Shared workspace (`isolation: none`). `subagent_type: general-purpose`.
`capability_mode: all`. `background: false`.

Never run spec, build, or review in the parent session. Never
reuse a subagent across phases.

## Auto-approve

Invoking this skill **is** the operator OK for spec APPLY step 3
and for applying behavior-preserving review simplifications
(step 5).

The spec subagent must apply the fold after the preview exists in
its return summary. It must not call `ask_user_question` for APPLY
or for fold-first. Fold-first: fold into the closest existing §V
row when the issue is the same topic; otherwise split as
`New row (orthogonal concept)` and record that in the commit body.

Audits that **bail** still stop the run. Auto-approve is not
auto-ignore.

## Eligible issue

Open issue in the cwd repo (no `--repo` slug). Skip when any of:

- it is a pull request
- an **open** PR body already has `Closes #N` / `Fixes #N` / `Resolves #N`
- `gh issue develop --list` already shows a branch **and** that
  branch has an open PR

Bare run (no `--all`): lowest issue number.

`--all` does not pick by number. See **Analysis** — the ordered
list is the pick-queue.

```bash
gh issue list --state open --limit 50 --json number,title,body,labels,url
```

Confirm one candidate:

```bash
gh issue view <N> --json number,title,body,labels,isPullRequest
gh pr list --state open --json number,body,title
```

No eligible issue → stop and say so. Do not invent work.

## Analysis (`--all` only)

Run once after preflight, before the first checkout. Load every
eligible issue (title, body, labels). Skim SPEC.md §T / §V only
enough to see overlap with live rows.

Edges only from evidence:

| Edge | When |
|---|---|
| `#A` before `#B` | B's body/labels say blocked-by / depends-on / after `#A` |
| `#A` before `#B` | Same primary file or module, and A is the lower number |
| guessed `#A` before `#B` | A is schema/API/foundation, B is a caller — mark **guessed** |

No evidence → keep numeric order among the remaining issues.
Do not invent edges to force a chain. Cycle → stop and name it.

Show the operator before step 2:

```
order: 12, 7, 19
#12  (independent)
#7   (independent)
#19  after #12  [guessed]
```

That list is the pick-queue for the rest of the run. Do not
re-analyze later; only drop or skip numbers.

## Safety

- Never commit or push on `main` / `master`.
- Never merge unless the operator picks **Approve merge now**
  (or the PR reaches `MERGED` on GitHub under the watch option).
- Push only after `make check` exits 0.
- Review `bug` findings stop the loop; do not push.
- `--all` on `/sdd:build` means every `.` row in §T on this branch,
  not only rows folded from this issue. That is intended.
- Stop the loop on any phase FAIL. Leave the branch; do not push
  a red tree.

## Procedure

Run from the **repo root**. One issue = one pass of steps 0–9.
`--all`: after step 0 run **Analysis** once, then pick from that
queue. Repeat from step 1 after a completed wait (or after PR
create when `--no-wait`).

todo_write one item per step below for the current issue.

### 0. Preflight

```bash
git rev-parse --show-toplevel
git status --porcelain
test -f SPEC.md
gh auth status
```

Stop if any of: not a git repo, `SPEC.md` missing, `gh` not
authenticated, working tree dirty (including untracked). The
operator must commit or stash first.

Resolve the sdd plugin root (see above). Missing plugin → stop.
Resolve the bundled review skill (see above). Missing → stop.

### 1. Pick issue

`$ARGUMENTS` is a number → that issue (still skip if ineligible,
unless it is already the checked-out issue branch).

`--all` → head of the remaining ordered list. Skip a head that
became ineligible (closed, or now has an open closing PR); do
not stop the loop for a skip.

Otherwise (no `--all`, no `N`) → oldest eligible issue.

Tell the user: `#N` + title + url. Under `--all`, also print
`remaining: <numbers>`.

### 2. BRANCH

Follow github skill **BRANCH**:

```bash
gh issue develop <N> --checkout
```

Reuse the current branch only when it is already the issue-linked
branch for `N`. After checkout:

```bash
git status --porcelain
git branch --show-current
```

Stop if checkout failed or the tree is dirty.

### 3. SPEC (isolated)

Spawn a subagent. Prompt must include:

- Absolute paths to `spec/SKILL.md` and `github/SKILL.md`
- Arguments: `github issue <N>`
- Auto-approve rule from this skill
- "Follow spec FOLD-IN — github issue. APPLY through write + commit.
   Return: issue number, §V/§T rows added or amended, commit sha,
   ADVISORY if the issue has no `## Acceptance`, FAIL + reason on
   audit bail. Do not start `/sdd:build`."

Parent: if the child returns FAIL, or SPEC.md has no new commit
for this fold, **stop**. Surface the preview/advisory.

### 4. BUILD (isolated)

New subagent. Prompt must include:

- Absolute path to `build/SKILL.md`, `ACCEPTANCE-GATE.md`,
  `github/SKILL.md`
- Arguments: `--all`
- "You are the main agent of this session. Follow the build skill.
   Return: each §T id flipped or left `.`, verify cmd + result,
   ACCEPTANCE-GATE verdict (ALLOW / BLOCK / ADVISORY), commit shas,
   FAIL classification if any. Do not push. Do not open a PR."

Parent: any task still `.` after the child, or verdict BLOCK, or
child FAIL → **stop**. Do not run step 5 as a pass.

### 5. REVIEW (isolated)

New subagent. Prompt must include:

- Absolute path to `$GROK_HOME/bundled/skills/review/SKILL.md`
- Arguments: `--branch <current-branch>`
- "You are the main agent of this session. Follow the review skill.
   Prioritize simplification: unnecessary abstraction, duplication,
   dead code, over-engineering. Still report bugs. Return: issue
   counts by severity, review_file path, empty-diff if none, FAIL +
   reason if the skill fails. Do not push. Do not open a PR. Do not
   edit project source."

Parent:

- child FAIL → **stop**
- empty-diff → continue to step 6
- any `bug` → **stop**; quote them
- `suggestion` issues → new subagent (do not reuse the reviewer):
  apply behavior-preserving simplifications from `review_file`,
  commit, return sha or NONE. Dirty tree or FAIL → **stop**.
- nits only → continue

Do not re-review after apply. `make check` is the next gate.

### 6. `make check`

Parent session, repo root:

```bash
make check
```

Non-zero → **stop**. Do not push. Quote the failing target.

### 7. Push

```bash
git push -u origin HEAD
```

Stop on push failure.

### 8. PR

After a successful push, open one GitHub PR for this issue.
Follow github skill **PR**. Load ACCEPTANCE-GATE first.

- BLOCK → open the PR **without** a close trailer; say why.
- ADVISORY → state the advisory, then open with `Closes #<N>`.
- ALLOW → `Closes #<N>`; post the Acceptance-evidence comment
  if build did not already.

PR body = steno (load `skills/steno/SKILL.md` when writing it):
summary + verification line (`make check` passed) + trailer.

```bash
gh pr create --title "<summary>" --body "<steno>"
```

Show the PR url.

### 9. Wait for merge approval

Skip this step when `--no-wait`.

`ask_user_question` (one question, recommended first):

- **Approve merge now** — follow github **MERGE**
  (`gh pr merge <n> --squash --delete-branch`). Re-run
  ACCEPTANCE-GATE; BLOCK still forbids merge.
- **I'll merge on GitHub** — start `monitor` (print only
  `DONE` / `FAILED`):

```bash
while true; do
  state=$(gh pr view <PR> --json state --jq .state 2>/dev/null || echo UNKNOWN)
  if [ "$state" = "MERGED" ]; then echo DONE; exit 0; fi
  if [ "$state" = "CLOSED" ]; then echo FAILED; exit 1; fi
  sleep 60
done
```

  `DONE` → continue. `FAILED` (unmerged close) → stop the loop;
  do not treat as resolved.
- **Stop here** — leave the PR open; end the run (even with `--all`).

Never pick merge yourself. Never call MERGE unless the operator
chose **Approve merge now**.

### 10. Next issue (`--all` only)

After MERGED (or after step 8 when `--no-wait`): drop the
finished `#` from the pick-queue.

```bash
git checkout main
git pull --ff-only
```

If `main` is not the default branch, use the repo default
(`gh repo view --json defaultBranchRef`). Then go to step 1
(next head of the remaining list). Empty list → stop.

FAIL still ends the run. Do not jump to the next number.

## Output

Per issue, one block:

```
#N <title>
branch: <name>
spec: <sha>  [ADVISORY?]
build: T<a>,T<b> x  gate=<ALLOW|BLOCK|ADVISORY>
review: <X>b <Y>s <Z>n  [applied <sha>|none|empty-diff]
make check: pass|fail
pr: <url>
merge: waiting|merged|stopped
```

End the run with `## Next` (1–5 items). Examples:

After a merged issue:

```
## Next

1. /github-resolve-issue --all
2. gh issue list --state open
```

After a PR left open:

```
## Next

1. open <pr-url>
2. /github-resolve-issue --no-wait
```

After FAIL:

```
## Next

1. git status
2. /sdd:spec <cause>   (only if the fail was spec-shaped)
3. /github-resolve-issue <N>
```
