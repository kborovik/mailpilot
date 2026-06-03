---
name: release
description: |
  Cut a SemVer release for `mailpilot`, build the `.whl` artifact, push tag and main, and publish a GitHub release with the wheel attached as a downloadable asset. Local-then-remote workflow — extends `/gh:release` semantics with `uv build` + `gh release create`. Triggers when the user says "release", "ship", "publish", "cut a version", "build the wheel", "deploy".
argument-hint: [patch|minor|major|x.y.z|retag-baseline]
allowed-tools: Bash(git *), Bash(uv build), Bash(uv lock), Bash(gh release *), Bash(ls dist/*), Read, Edit
model: sonnet
---

Cut a SemVer release for `mailpilot`, build the `.whl` artifact, push, and publish a GitHub release with the wheel attached.

Spec: §V.62 (project release flow contract).

## Scope

Single-package repo. Manifest = `pyproject.toml [project].version`. Build backend = `uv_build`. Deploy artifact = `dist/mailpilot-<x.y.z>-py3-none-any.whl` (single platform-agnostic wheel; `uv_build` emits one).

Procedure §1–§12 inherits `/gh:release` skill verbatim (kept inline so this skill is self-contained — `/gh:release` plugin cmd unaffected). Procedure §13–§15 extend with push + build + publish.

Tag format: `v<x.y.z>` (e.g. `v0.1.0`, `v1.2.3`). SemVer only — ⊥ pre-release suffixes.

## Procedure

### §1 Parse $ARGUMENTS

- ⊥ arg → auto bump (∨ baseline-mode if first release).
- `patch` ∨ `minor` ∨ `major` → bump direction override.
- `x.y.z` (literal SemVer) → pin exactly. Must match `^[0-9]+\.[0-9]+\.[0-9]+$`. Permits downgrade.
- `retag-baseline` → recovery mode. Drops every prior tag matching §3 pattern ∧ retags HEAD at current manifest version. Requires ≥ 1 prior tag. Manifest unchanged.

### §2 Detect manifest

Manifest = `pyproject.toml`. Read `[project].version`. Invalid SemVer → error.

### §3 Find last tag

```
git tag --list "v*" --sort=-v:refname | head -n 1
```

⊥ tag → first release (baseline mode applies in §6).

### §4 Read current version

Read `[project].version` from `pyproject.toml`. Invalid SemVer → error.

### §5 Collect commits in range

- Range: `<last-tag>..HEAD` if tag exists, else full history.
- **Retag-baseline mode** → range = full history regardless of prior tags.
- `git log <range> --pretty=format:"%H%x09%s"`
- **Exclude** commits whose subject matches `^chore(\([^)]+\))?: release v[0-9]+\.[0-9]+\.[0-9]+` — skill's own release commits ⊥ count toward bump.
- ⊥ commits remaining → exit cleanly w/ this exact shape (bypassed in retag-baseline mode):

  ```
  Nothing to release.
    Range:                   <last-tag>..HEAD   (or "full history" if no prior tag)
    Last tag:                <last-tag-or-NONE>
    Manifest @ version:      pyproject.toml @ <current-version>
    Commits in range:        <total>
    Filtered self-release:   <n>   (matches §5 regex)
    Commits remaining:       0

  Next steps:
    - Add commits, then re-run.
    - To force a release with no new commits, pass an explicit `<x.y.z>` arg.
    - To recover a wrong prior tag, pass `retag-baseline`.
  ```

- ⊥ side effects beyond stdout.

### §6 Determine target version

- Arg = `retag-baseline` → target = current manifest version. Manifest unchanged. Proceeds to drop prior tags in §11.5.
- Arg = `x.y.z` → target = `x.y.z`. Skip auto-detect.
- Arg = `patch` / `minor` / `major` → bump from current. Skip auto-detect.
- ⊥ arg ∧ ⊥ prior tag → **baseline mode**: target = current version. Tags the version already declared.
- ⊥ arg ∧ prior tag exists → auto-detect from commit subjects:
  - Any `<type>(<scope>)?!:` ∨ body containing `BREAKING CHANGE` → **major**
  - Else any `feat(<scope>)?:` → **minor**
  - Else → **patch**

### §7 Compute next version

(Skip if explicit `x.y.z`.)

- patch: `x.y.z` → `x.y.(z+1)`
- minor: `x.y.z` → `x.(y+1).0`
- major: `x.y.z` → `(x+1).0.0`
- baseline: target = current

### §8 Render release notes

Steno-styled, grouped by Conventional Commits type:

- **Features** ← `feat`
- **Fixes** ← `fix`
- **Other** ← `refactor` / `docs` / `chore` (non-release) / `test` / `perf` / `build` / `ci` / `style`
- **Uncategorized** ← anything not matching `<type>(<scope>)?:` prefix

Each entry: bullet with subject minus `<type>(<scope>):` prefix. Keep scope only if it disambiguates.

Preserve `#refs`, paths, identifiers, SHAs verbatim. Apply `core:steno` skill — fragments, ⊥ filler.

Baseline mode + only-init-commits → release notes may be empty / sparse. Acceptable.

### §9 Confirm before mutation

Render breakdown so bump derivation is auditable. Required fields:

- **Manifest**: `pyproject.toml @ <current-version>`.
- **Range**: `<last-tag>..HEAD` ∨ `<full history>` if baseline.
- **Commit counts by Conventional Commits type**: e.g. `feat:0  fix:0  refactor:1  docs:0  chore:0  breaking:0  uncategorized:0`. Cover all types observed in range.
- **Filtered self-release commits**: count of commits matching §5 self-release regex. Show count even if zero.
- **Bump derivation**: explicit one-liner showing rule fired — e.g. `0 breaking + 0 feat → patch (default)`, `1 breaking → major`, `arg "x.y.z" → pinned`, `no prior tag & ⊥ arg → baseline (no bump)`, `arg "retag-baseline" → recovery (no bump, manifest unchanged)`.
- **Target tag**: `v<x.y.z>`.
- **Tags scheduled for deletion** (retag-baseline only): list every prior tag matching §3 pattern.
- **Lockfile refresh**: `uv lock` → `uv.lock` staged with `pyproject.toml` (skipped in baseline ∨ retag-baseline mode).
- **Push refs**: `origin main`, `origin v<x.y.z>` (skipped in retag-baseline mode → see §13).
- **Wheel path**: `dist/mailpilot-<x.y.z>-py3-none-any.whl`.
- **GitHub release cmd**: `gh release create v<x.y.z> --verify-tag --notes-from-tag dist/mailpilot-<x.y.z>-py3-none-any.whl`.
- **Rendered release notes** (steno-styled, grouped per §8).

Wait for user confirmation. ⊥ confirm → exit, ⊥ side effects.

### §10 Bump version in manifest

(Skip if baseline mode ∨ retag-baseline mode — manifest already at target.)

- Edit `pyproject.toml` → set `[project].version` = target.
- Run `uv lock` → refresh `uv.lock` so the `mailpilot` package version field tracks the manifest. Why: stale lockfile causes the next `uv run` to mutate `uv.lock` outside a release commit, leaking the bump into an unrelated change.
- Stage: `git add pyproject.toml uv.lock`.

### §11 Commit

(Skip if baseline mode ∨ retag-baseline mode.)

```
git commit --cleanup=verbatim --message "$(cat <<'EOF'
chore: release v<x.y.z>
EOF
)"
```

### §11.5 Delete prior tags

(Retag-baseline mode only.)

For every tag listed in §9 "Tags scheduled for deletion":

```
git tag --delete <tag>
```

Local deletion only at this step. Remote deletion handled inline at §13.

### §12 Init annotated tag w/ notes inline

`--cleanup=verbatim` mandatory (release notes use `## Features` / `## Other` headers that git's default cleanup would strip as comments):

```
git tag --annotate --cleanup=verbatim v<x.y.z> --message "$(cat <<'EOF'
v<x.y.z>

<release notes>
EOF
)"
```

### §13 Push

```
git push origin main
git push origin v<x.y.z>
```

Retag-baseline mode → also run, for each tag dropped in §11.5:

```
git push origin :refs/tags/<old-tag>
```

Push failures on `origin main` (e.g. branch protection, remote ahead) → bail w/ remediation note. Tag is created locally but ⊥ rolled back; user resolves remote state then re-pushes manually.

### §14 Build wheel

```
uv build
```

Assert `dist/mailpilot-<x.y.z>-py3-none-any.whl` exists. ⊥ exists → bail with `ls dist/` output (build emitted unexpected name pattern → spec drift on §V.62). ⊥ retry blindly.

### §15 Publish GitHub release

```
gh release create v<x.y.z> --verify-tag --notes-from-tag dist/mailpilot-<x.y.z>-py3-none-any.whl
```

`--verify-tag` → fail if remote tag absent (catches §13 push gap). `--notes-from-tag` → reuse tag annotation rendered in §12 (single source of truth for release notes).

`gh release create` failure → bail w/ stderr. Tag and wheel persist; user re-runs `gh release create v<x.y.z> ...` manually after fixing the cause.

### §16 Echo result

- Tag name, target SHA, computed bump.
- Re-print rendered annotation.
- GitHub release URL (from `gh release create` stdout).
- Wheel path on local filesystem (`dist/mailpilot-<x.y.z>-py3-none-any.whl`).
- Deploy hint: `gh release download v<x.y.z> --pattern '*.whl'` to fetch the artifact for installation.

## Style

Apply `core:steno` skill to release notes. Drop articles ∧ filler, fragments ∧ bullets, preserve identifiers / paths / `#refs` / SHAs verbatim. Tag name ∧ version string fixed → ⊥ compress.

## Requirements

- Manifest = `pyproject.toml`. Build backend = `uv_build` (declared in `[build-system]`). Single wheel emitted: `mailpilot-<x.y.z>-py3-none-any.whl`.
- `uv.lock` ! re-locked alongside the manifest bump and committed in the same `chore: release v<x.y.z>` commit. Why: `uv.lock` records the workspace package version; leaving it stale means the next `uv` invocation rewrites it outside any release commit.
- SemVer only — `x.y.z`, ⊥ pre-release suffixes.
- Annotated tag (`-a` / `--annotate`), ⊥ lightweight.
- All `git tag --annotate` ∧ `git commit` calls pass `--cleanup=verbatim` — git's default `commit.cleanup=strip` would silently drop `#`-prefix lines (release-note section headers) from messages.
- Confirm before mutation. ⊥ silently rewrite version files. Confirm step ! list tags scheduled for deletion in retag-baseline mode, push refs, wheel path, gh release create cmd.
- Self-release commits (`chore: release v*`) excluded from auto-detect scan.
- Baseline mode for first release: ⊥ bump if no prior tag ∧ ⊥ explicit arg.
- Retag-baseline recovery: explicit `retag-baseline` arg drops every prior tag matching pattern ∧ retags HEAD at current manifest version. Requires ≥ 1 prior tag. Manifest unchanged.
- `gh auth status` must report a logged-in user with `repo` scope before §15. ⊥ logged in → bail at §9 confirm step (caller reruns after `gh auth login`).
- `dist/` writable; stale wheels in `dist/` from prior runs are tolerated (uv build does not clean) — §14 only asserts presence of the target wheel.
- `git push origin main` must succeed for §15 to be runnable. Branch protection / remote-ahead → resolve manually.

## Non-goals

- Monorepo orchestration. Single-package only.
- `CHANGELOG.md` rendering — release notes live in tag annotation ∧ GitHub release body.
- sdist (`.tar.gz`) upload — wheel only. uv build emits both; only the wheel is attached as a release asset.
- Multi-platform wheels — `py3-none-any` only.
- Pre-release / RC tags.
