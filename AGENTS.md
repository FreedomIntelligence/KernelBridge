# Working in this repository

## Landing changes on main

- Work on a branch, usually in a `git worktree`. Land it from the main checkout with `git merge --ff-only BRANCH`, or with a merge commit when histories diverged.
- Never move `main` with `git update-ref` or `git branch -f` while the main checkout is on it. Its files stop matching `HEAD`, and committing them reverts other work. This happened on 2026-11-11 and broke the TUI until a three-way merge repaired it.
- Before landing, run all three:
  - `python3 source/scripts/update_manifest.py`. Never edit `PIN_MANIFEST.json` by hand; the launcher refuses to start on a mismatch.
  - `python3 install.py --verify-only`.
  - `npm test` in `source/apps/agent`.
- Pi patch changes: rebuild `patches/pi-coding-agent-1.5.0-host-actions.{patch,json}` from the locked Pi source with the patch builder, and check all three digests.

## Running a worktree

- Clone dependencies without copying data: `cp -cR source/apps/agent/node_modules WORKTREE/source/apps/agent/`.
- Bind the development runtime: `PYTHON -B source/scripts/develop.py`, where `PYTHON` is the interpreter named in the main checkout's `.development/python`. Do not use `-I`; it breaks the script's own imports.
- `.validation/tui-install-ui` and `.validation/ui-test-deps` hold that interpreter and the PTY test dependencies. Do not delete them during disk cleanup.

## Acceptance

- `validation/tui/run.py --output NEW_DIR` runs the local suite. Every check is passed, flaky, failed or environmental; only passed counts. Deadlines scale with `KB_TIMEOUT_SCALE`.
- Do not change files in a worktree while a PTY run is using it. The launcher re-checks sources against the manifest on every start.
- Record results in `validation/tui/ACCEPTANCE.md`, with failed attempts as well as passes.
