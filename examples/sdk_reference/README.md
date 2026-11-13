# Installed SDK reference snapshots

These optional tools let an agent inspect the declarations and public templates
of the SDK selected for its task. They work with CANN, CUDA, and other SDKs;
there are no operator-specific sources, API answers, or candidate changes.
They read a provisioned local snapshot, without network access, SSH, compilation,
SDK imports, or device initialization. Keep the snapshot outside disposable
worktrees: resumed sessions retain its manifest identity and still need its files.

## Provision once, before starting a task

Run the exporter on the machine that contains the selected SDK. Select public
directories by their role, not by a particular operator or error message:

```sh
python3 -B examples/sdk_reference/snapshot.py \
  --root /opt/vendor/sdk-version \
  --output /persistent/sdk-snapshot \
  --name vendor-sdk --version VERSION --target TARGET \
  --include include --include compiler/public-headers
```

The output contains `manifest.json` and regular files beneath `files/`. Its
manifest records the declared version and target, original and canonical SDK
roots, directory and extension selection, excluded non-text types, original
logical paths and symlink targets, and every included file's size and SHA256.
The snapshot ID hashes that complete manifest body. Export rechecks the source
inventory and file contents before publication. Explicit limits reject an
incomplete export rather than silently returning a partial reference library.
Internal source symlinks are resolved and copied to regular files; links escaping
the SDK root, cyclic directories, selected files containing NUL, and file changes
are rejected. Non-UTF8 public headers are retained byte-for-byte; the exporter
does not guess their character encoding or transcode their contents. A failed
export leaves `export-failure.json`, not a usable manifest.

For a remote SDK, the trusted provisioning process transports this entire
directory to persistent local storage and verifies the manifest and all files.
The destination filesystem must preserve every original path distinctly,
including names that differ only by case. On a case-insensitive host, provision
such snapshots on persistent case-sensitive storage; do not rename or merge
source files. Keep that storage available when resuming a session.
The exporter and agent tools do not implement an SSH transport. Version strings
and optional `--origin-json` facts are provenance declarations; the provisioner must
bind them to its actual toolchain/deployment receipts. A snapshot is not proof
that a candidate was compiled or executed against that installation.

Configure the tool resource explicitly in the codegen or operator profile:

```json
{
  "sdk_reference": {
    "resources": [
      {"id": "vendor", "manifest": "/persistent/sdk-snapshot/manifest.json"}
    ]
  }
}
```

`prepare(profile, output, python)` validates the full corpus, copies the manifest
into the frozen tool inputs, and registers `sdk_search` and `sdk_read` through the
existing pinned business-tool gateway. The SDK corpus is not copied into the
task workspace or shell snapshots. Parent sessions and authorized read-only
subagents receive the same registered resources; absent configuration adds no
tools. The worker configuration contains the resulting business tools, not the
unconsumed `sdk_reference` setting.

## Read the actual evidence

* `sdk_read({})` discovers registered resource IDs, captured versions, targets,
  provenance, and file counts. `sdk_read({"resource":"vendor"})` describes one.
* `sdk_search({"resource":"vendor","mode":"path","query":""})` lists
  registered paths. A literal path substring and `path_glob` narrow this list.
* `sdk_search({"resource":"vendor","query":"SomeType"})` searches literal
  UTF-8 query bytes in the original file bytes, including non-UTF8 files. Each
  returned match includes the file hash, line, byte offset and a ready-to-use
  `sdk_read` entry. The tool verifies every scanned file. A snippet's `encoding`
  specifies whether its single `text` field is UTF-8 or base64; `text_size_bytes`
  and `text_sha256` identify those original snippet bytes without replacement.
* `sdk_read({"resource":"vendor","path":"include/header.h","offset":0,
  "limit":8192})` returns a bounded original page. Offsets and limits count
  bytes. Pages end at a complete UTF-8 character; an explicitly misaligned start
  or a limit shorter than one character instead uses `encoding=base64`, with
  base64 in the single `text` field. Pages containing invalid UTF-8 also use
  base64. The full-file SHA256 stays attached; `source_utf8_valid` reports whether
  the complete original file passed UTF-8 validation, not an inferred codec.

Content search reports `query_encoding=utf-8` and counts non-UTF8 files actually
verified on that page. ASCII API spellings are searched as their literal bytes;
no encoding conversion is performed. A complete search with no matches means
that this byte sequence was absent from the reported scope. It does not prove
that equivalent non-ASCII characters are absent under another source encoding.
Use path search and byte pages to inspect such sources without discarding them.

Source pages and retained JSON reports use the existing artifact-page contract:
`text`, `encoding`, `offset`, `next_offset`, `size_bytes`, `sha256`, `truncated`,
`eof`, `read` and `next_read`. The ordinary context projection can shorten a page
for a read-only subagent without losing text or advancing past the visible
bytes. Follow `next_read` to continue. Search structures are stored in
`search.json` and resource discovery in `reference.json`; their returned JSON
pages do not duplicate a large structured result outside `text`.

Path searches inspect pinned path metadata without reading every file body;
only returned matching files are opened and hash-verified. The response reports
`path_entries_scanned`, `contents_verified` and `content_scanned_files`
separately. Start with path search to locate relevant headers. Content searches
inspect at most 128 files and 16 MiB per page. Either mode returns at most 50
matches and verifies at most 16 MiB of file bodies per page.
`complete=false` and `next_cursor` mean the remaining scope has not been searched.
Reuse the same resource/query/mode/glob with that cursor; an empty partial page
does not establish that a declaration is absent. These are per-call transport
bounds, not a turn or total-token budget.

Every query/page retains `request.json`, `result.json`, and a hash-bound receipt;
searches retain `scan.json` and `search.json`, resource discovery retains
`reference.json`, and source reads retain `page.bin`. The receipt binds original
page artifact hashes; no separate displayed-page hash becomes stale when the
context projector shortens text.
`sdk_read({"run":"RUN_ID"})` returns that receipt's manifest.
`sdk_read({"run":"RUN_ID","path":"result.json","offset":0,"limit":8192})`
reads the original response without rescanning the SDK. Do not combine `run`
with a resource selection. The existing Gateway and Journal remain responsible
for effect admission and replay. An incomplete original receipt is reported as
unknown, not silently executed again.

Runtime file opens reject symlink components, nonregular files, path escapes,
unregistered paths, size changes and hash changes. The agent cannot pass a host,
SDK root, command, or arbitrary filesystem path. Reference content remains
untrusted data and cannot expand tool permissions. SDK access does not weaken
candidate, oracle, measurement, process-isolation or unknown-effect checks.
