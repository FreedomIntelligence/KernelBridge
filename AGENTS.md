# Working in this repository

## Development and release branches

- `dev` contains the complete development tree: implementation, tests, plans and audit notes. Start ordinary work from `dev`, usually on a branch in a separate `git worktree`, and land it on `dev` after validation.
- `main` contains only runtime and installation files, including instructions and templates consumed by the product. Never merge `dev` directly into `main`: that would restore development files to the release tree.
- Export a committed `dev` revision with `python3 -B source/scripts/export_release.py --ref HEAD --output /absolute/new-bundle`. The output must not exist. Rules and templates are versioned under `source/scripts/release/`; see `docs/architecture/release-workflow.md`.
- Review and install the exported tree, then apply its tracked file set to a release branch based on the latest `origin/main`. Land that release branch from the main checkout with `git merge --ff-only BRANCH`, or a reviewed merge when histories diverge. Re-fetch before pushing; do not force-push.
- Never move `main` with `git update-ref` or `git branch -f` while the main checkout is on it. Its files stop matching `HEAD`, and committing them reverts other work. This happened on 2026-11-11 and broke the TUI until a three-way merge repaired it.
- Before committing development changes, run all three:
  - `python3 source/scripts/update_manifest.py`. Never edit `PIN_MANIFEST.json` by hand; the launcher refuses to start on a mismatch.
  - `python3 install.py --verify-only`.
  - `npm test` in `source/apps/agent`.
- Before releasing, additionally verify the exported manifest and run a fresh installation and launcher smoke check. Keep test tools and their output on `dev` or outside the release tree. Passing development tests alone does not prove the exported installation is complete.
- Pi patch changes: rebuild `patches/pi-coding-agent-1.5.0-host-actions.{patch,json}` from the locked Pi source with the patch builder, and check all three digests.

## Running a worktree

- Clone dependencies without copying data: `cp -cR source/apps/agent/node_modules WORKTREE/source/apps/agent/`.
- Bind the development runtime: `PYTHON -B source/scripts/develop.py`, where `PYTHON` is the interpreter named in the main checkout's `.development/python`. Do not use `-I`; it breaks the script's own imports.
- `.validation/tui-install-ui` and `.validation/ui-test-deps` hold that interpreter and the PTY test dependencies. Do not delete them during disk cleanup.

## Acceptance

- `validation/tui/run.py --output NEW_DIR` runs the local suite. Every check is passed, flaky, failed or environmental; only passed counts. Deadlines scale with `KB_TIMEOUT_SCALE`.
- Do not change files in a worktree while a PTY run is using it. The launcher re-checks sources against the manifest on every start.
- Record results in `validation/tui/ACCEPTANCE.md`, with failed attempts as well as passes.
