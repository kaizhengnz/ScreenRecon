# ScreenRecon — project conventions

## Commit / PR / prose style
- No AI-tooling attribution in commits or PR bodies (no `Co-Authored-By: Claude`, no "Generated with Claude Code").
- Prefer neutral phrasing in user-facing prose: "vision API" / "the model" rather than "Claude API". Model IDs in code/config (e.g. `claude-opus-5`) stay — they are SDK-required.

## Testing
- Tests may not touch the network or the screen. Everything is driven through fakes / injected boundaries. If a new behavior seems to need a real API call or a real capture to test, restructure the seam instead.

## Credential handling
- Never log or print a secret in full — the established convention is first 8 characters only.
- Third-party HTTP libraries embed the request URL (which contains the Telegram bot token) in their exception text. Route third-party error strings through the existing scrubbing layer before they reach stdout, logs, or tracebacks.
- The setup wizard reads credentials without echoing them; keep it that way.

## Docs stay in sync with behavior
- `README.md` is the authoritative user-facing description. When changing observable behavior (CLI flags, config fields, output format, platform quirks, error messages users see), update the README in the same change.
- Feature additions and behavior changes get a row in the **Revision history** table of `docs/ScreenRecon_Design.md`. The **Document version** row in the header table is not bumped manually — it is auto-synced to `__version__` by `release-prepare.yml` on each release, so the design doc always states which code version it describes.
- No separate `CHANGELOG.md` — only introduce one if the Revision history table outgrows itself.

## Design doc header table
- The metadata table at the top of `docs/ScreenRecon_Design.md` carries only current-state rows (Project, Document version, Date, Status, Audience).
- Draft history, "Supersedes", language lineage → route into the Revision history table further down, never into the header.

## Workflow
- **All changes go through a pull request.** No direct commits or pushes to `main` — not even for typos, doc-only edits, or config tweaks. If a change is worth making, it is worth a branch and a PR.
- Every branch is tied to a tracking issue. If no issue exists for the change, open one first.
- Branch naming: `SR-<issue#>-<short-slug>` (e.g. `SR-2-drag-picker`). `SR` = ScreenRecon; slugs are lowercase kebab-case.
- PR title starts with the branch prefix: `SR-<issue#> <description>` (e.g. `SR-2 Add drag-to-select region picker`). Mirrors the branch name so issue → branch → PR are visually linked at a glance.
- No punctuation in commit titles, PR titles, or branch names. No `:` (drops conventional-commits prefixes like `feat:` / `fix:` / `refactor:`), no `,`, no `.`, no `_`, no quotes. Titles use plain prose (conjoin with "and", not ","); branch slugs are lowercase kebab-case — letters, digits, and hyphens only.
- PR body contains `Closes #<n>` so merging auto-closes the issue.
- On a feature branch, add new commits for iterative changes — do not `git commit --amend` and force-push. The PR's Commits tab should show the progression of decisions (add rule, then implement, then adjust, etc.). Squash merge at the end still collapses everything into a single commit on `main`.
- Merge method: **squash merge** — keeps `main` linear, one commit per issue.
- The single exception: release-plumbing PRs opened by `github-actions[bot]`. They are not tied to a tracking issue, use a `release/vX.Y.Z` branch (not the `SR-<issue#>-<slug>` pattern), and keep the `chore: release vX.Y.Z` commit and PR title (colon retained so `release-publish.yml`'s detection regex stays anchored on it). See **Versioning & release** below for the flow.

## Versioning & release
- Version source: `__version__` in `src/screenrecon/__init__.py`. Hatchling reads it dynamically. Never edit this directly to bump — always route through the release workflow.
- Releases: trigger **Prepare release PR** (`release-prepare.yml`) via GitHub Actions → `Run workflow` (`workflow_dispatch`). It bumps `__version__`, opens a release PR from `release/vX.Y.Z`, and enables auto-merge. Once the PR merges, **Publish to PyPI** (`release-publish.yml`) fires on the merge commit and runs test → build → tag → publish.
  - `bump` input: `patch` (default) | `minor` | `major`
  - `patch` bumps are routine. `minor` / `major` require the user to consciously pick that option — do not suggest them automatically.
