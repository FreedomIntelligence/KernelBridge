# Development and release trees

`dev` is the complete development branch. It retains tests, design documents,
debugging tools and release tooling. `main` contains the runtime and installation
tree. Product-consumed agent instructions, task templates, native build inputs,
licenses and the README files required by Python packaging remain runtime inputs.

## Export a revision

Commit and validate changes on `dev`, then run:

```sh
python3 -B source/scripts/export_release.py --ref HEAD --output /absolute/new-bundle
```

The exporter reads files, rules, templates and the manifest generator from the
selected Git commit. Uncommitted edits, untracked files and ignored files are not
included. The output must not exist and its parent must exist. Links, submodules and special entries are rejected. Git archive attributes do
not alter the selected runtime blobs. A failed export retains its partial output for
inspection; use a different output directory for another attempt.

The exporter does not change a branch, commit files or push to a remote. It:

1. Selects only the runtime and installation paths declared in
   `source/scripts/release/rules.json`.
2. Applies the release README and launcher templates.
3. Removes development npm scripts and dependencies, strips Python test/lint/type
   checker settings and optional development dependencies, and filters native
   resource inventories to the retained payload. Python build metadata and runtime
   optional dependencies are checked for preservation.
4. Runs the committed manifest generator in release mode. File hashes are
   regenerated; development history and old validation metadata are omitted.
5. Runs `install.py --verify-only` on the output.

The source commit is recorded in the generated manifest. Identical source commits
produce identical file bytes and manifest contents; no export timestamp or local
path is recorded in the bundle. The console result identifies the output directory.

## Validate and promote

Run the normal development acceptance checks before exporting. Then install the
export in a fresh directory using explicit Python, Node and npm paths, and check
ordinary startup and the affected runtime features. `--verify-only` establishes
source integrity; it does not establish that dependencies install or the TUI works.
Device performance and correctness evidence are separate from release checks.

Fetch `origin/main`, create a release branch from it, and apply only the verified
exported source file set. Ignore installation output such as `runtime/`,
`.installation/` and `node_modules/`. Review deletions as well as additions. Commit
the release tree, re-fetch to detect concurrent main changes, and land through a
normal fast-forward or reviewed merge in the main checkout. Do not force-push or
move an active worktree's branch with `update-ref` or `branch -f`.

Do not merge the complete `dev` branch directly into `main`. Changes made while
qualifying a release must also be applied to `dev`; update release templates and
rules there before producing the next export. Keep session archives, trial receipts
and device evidence outside the release payload. Excluding those files from a
release never authorizes deleting their original archives.

## Updating publication rules

Root files are an allowlist. Runtime `source/` and `examples/` are included with
explicit development exclusions. The release launcher and root README have
separate templates because they intentionally do not expose development commands.
The four Python package README files are kept as build inputs. Other Markdown
instructions required by product tools are retained.

When moving or removing a file, inspect imports, launcher references, dynamic
loading, native resource manifests and package metadata. Preserve legal provenance
and licenses. Regenerate `PIN_MANIFEST.json` with the tool, never by editing hashes
manually. `source/scripts/update_manifest.py --root /bundle --release` supports
release qualification while keeping the generator itself on `dev`.

## Initial separation validation (2026-11-13)

- Preserved the complete `eb95ff8` development tree on `dev` before preparing the release tree.
- Moved the two CANN runtime adapters from validation into `examples/cann/adapters`; the precision regression passed all 3 checks.
- Export and portable-configuration regressions: 25 passed. Coverage includes committed-source isolation, exclusive output creation, manifest identities, explicit SSH selection, local execution, profiler configuration and refusal before external effects.
- JavaScript and TypeScript suite: 235 total, 232 passed, 0 failed, 3 conditional skips (two original-session fixtures and a case-sensitive-filesystem check).
- A preliminary JS run skipped the two Python-backed SDK cases because this new dev worktree had no bound runtime. After binding the existing Python installation to freshly built development wheels, both cases passed in the final run.
- The first development bind failed because the shell selected an old Node. It succeeded with the existing Node 22.19 installation on PATH. Both logs remain in `.validation/release-boundary/`.
- Release installation and launcher acceptance are recorded separately; development test results are not NPU performance qualification.
