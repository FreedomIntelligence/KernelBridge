"""Journal read path: verify only appended records, never trust a changed history.

Covers the verified-prefix cache in kernelcat_executor.state.journal: O(appended)
verification, fresh rows per caller, truncation, recomputed rewrites, same-size
middle edits (caught by the periodic full verification), file replacement,
torn tails, read-only transactions, a concurrent writer process, failed appends
and equality with a full rebuild. No model service or device is used.
"""
import json
import os
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
for path in [*ROOT.glob("source/packages/*/src"), ROOT / "source/apps/worker/src"]:
    sys.path.insert(0, str(path))

from kernelcat_executor.contracts.models import Conflict, Policy, canonical, digest
from kernelcat_executor.profiles.store import AgentJournal
from kernelcat_executor.state import journal as journal_module


def note(index, text="x"):
    return lambda rows: ({"kind": "note", "index": index, "text": text}, None)


class JournalReadPathTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="kb-journal-")
        self.path = Path(self.temp.name).resolve() / "events.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.journal = AgentJournal(self.path)
        self.journal.initialize(100, Policy(max_steps=10))
        for index in range(40):
            self.journal._transaction(note(index, "payload-" + str(index)))

    def tearDown(self):
        self.temp.cleanup()

    def count_digests(self, fn):
        calls = []
        real = journal_module.digest
        with mock.patch.object(journal_module, "digest", side_effect=lambda value: calls.append(1) or real(value)):
            result = fn()
        return len(calls), result

    def rewrite(self, rows):
        """Writes rows with a freshly computed chain (what an attacker able to rewrite would do)."""
        previous, lines = "0" * 64, []
        for seq, row in enumerate(rows):
            body = {key: value for key, value in row.items() if key != "digest"}
            body.update(seq=seq, prev=previous)
            body["digest"] = previous = digest(body)
            lines.append(canonical(body) + "\n")
        self.path.write_text("".join(lines))

    def test_reads_verify_only_appended_records(self):
        cold, rows = self.count_digests(self.journal.events)
        self.assertEqual(len(rows), 41)
        warm, _ = self.count_digests(self.journal.events)
        self.assertEqual(warm, 1, "only the anchor record is re-hashed when nothing was appended")
        writer = AgentJournal(self.path)
        for index in range(5):
            writer._transaction(note(100 + index))
        grown, rows = self.count_digests(self.journal.events)
        self.assertEqual(grown, 1 + 5, "the anchor plus the appended records")
        self.assertEqual([row["index"] for row in rows[-5:]], list(range(100, 105)))
        fresh = AgentJournal(self.path)
        full, _ = self.count_digests(fresh.events)
        self.assertEqual(full, 46, "a process without a verified prefix checks every record")

    def test_callers_cannot_mutate_the_verified_cache(self):
        rows = self.journal.events()
        rows[1]["kind"] = "tampered"; rows[2]["text"] = None; rows.append({"kind": "forged"})
        snapshot = self.journal.snapshot(); snapshot["policy"]["max_steps"] = 999
        again = self.journal.events()
        self.assertEqual(again[1]["kind"], "note"); self.assertEqual(again[2]["text"], "payload-1"); self.assertEqual(len(again), 41)
        self.assertEqual(self.journal.snapshot()["policy"]["max_steps"], 10)

    def test_truncation_of_verified_history_is_refused(self):
        self.journal.events()
        lines = self.path.read_bytes().splitlines(keepends=True)
        self.path.write_bytes(b"".join(lines[:-3]))
        with self.assertRaisesRegex(Conflict, "truncated"):
            self.journal.events()

    def test_recomputed_rewrite_changes_the_anchor(self):
        self.journal.events()
        rows = [json.loads(line) for line in self.path.read_text().splitlines()]
        rows[20]["text"] = "rewritten with a longer value so the file grows"
        self.rewrite(rows)
        with self.assertRaisesRegex(Conflict, "verified history changed"):
            self.journal.events()

    def test_same_size_middle_edit_is_caught_by_full_verification(self):
        self.journal.events()
        data = self.path.read_bytes()
        edited = data.replace(b'"payload-17"', b'"PAYLOAD-17"', 1)
        self.assertEqual(len(edited), len(data))
        self.path.write_bytes(edited)
        # Documented window: the anchor is intact, so the incremental read passes.
        self.assertEqual(len(self.journal.events()), 41)
        with mock.patch.object(journal_module, "FULL_VERIFY_SECONDS", 0.0):
            with self.assertRaisesRegex(Conflict, "integrity"):
                self.journal.events()
        with self.assertRaisesRegex(Conflict, "integrity"):
            AgentJournal(self.path).events()

    def test_replacement_must_extend_the_verified_history(self):
        self.journal.events()
        rows = [json.loads(line) for line in self.path.read_text().splitlines()]
        extended = self.path.with_name("extended.jsonl")
        extended.write_bytes(self.path.read_bytes())
        AgentJournal(extended)._transaction(note(500))
        os.replace(extended, self.path)
        self.assertEqual(self.journal.events()[-1]["index"], 500, "a replacement that extends the history is accepted")
        rows[5]["text"] = "other history"
        other = self.path.with_name("other.jsonl")
        self.rewrite(rows)  # writes self.path in place; move it aside as the replacement
        os.replace(self.path, other); os.replace(other, self.path)
        with self.assertRaisesRegex(Conflict, "does not extend|verified history"):
            self.journal.events()

    def test_torn_tail_is_still_refused(self):
        self.journal.events()
        with self.path.open("ab") as stream:
            stream.write(b'{"kind": "half')
        with self.assertRaisesRegex(Conflict, "torn journal tail"):
            self.journal.events()

    def test_read_only_transactions_cannot_append(self):
        size = self.path.stat().st_size
        with self.assertRaises(RuntimeError):
            self.journal._transaction(note(999), shared=True)
        self.assertEqual(self.path.stat().st_size, size)

    def test_failed_append_leaves_the_cache_consistent(self):
        before = self.journal.events()
        with mock.patch.object(journal_module.os, "write", side_effect=OSError(28, "No space left on device")):
            with self.assertRaises(OSError):
                self.journal._transaction(note(700))
        self.assertEqual(self.journal.events(), before)
        self.journal._transaction(note(701))
        self.assertEqual(self.journal.events()[-1]["index"], 701)

    def test_concurrent_writer_process(self):
        self.journal.events()
        script = (
            "import sys; sys.path[:0] = %r\n"
            "from pathlib import Path\n"
            "from kernelcat_executor.profiles.store import AgentJournal\n"
            "j = AgentJournal(Path(%r))\n"
            "for i in range(150): j._transaction(lambda rows, i=i: ({'kind': 'note', 'index': 1000 + i}, None))\n"
        ) % ([str(p) for p in [*ROOT.glob("source/packages/*/src"), ROOT / "source/apps/worker/src"]], str(self.path))
        writer = subprocess.Popen([sys.executable, "-B", "-c", script])
        seen = []
        while writer.poll() is None:
            seen.append(len(self.journal.events()))
        self.assertEqual(writer.wait(), 0)
        self.assertEqual(seen, sorted(seen), "the reader only ever moves forward")
        self.assertEqual(len(self.journal.events()), 41 + 150)
        self.assertEqual(self.journal.snapshot(), AgentJournal(self.path).snapshot())

    def test_incremental_state_equals_a_full_rebuild(self):
        rng = random.Random(7)
        writer = AgentJournal(self.path)
        for step in range(60):
            for _ in range(rng.randrange(0, 4)):
                writer._transaction(note(2000 + step, "v" * rng.randrange(0, 50)))
            self.assertEqual(self.journal.events(), AgentJournal(self.path).events())
        self.assertEqual(self.journal.snapshot(), AgentJournal(self.path).snapshot())


if __name__ == "__main__":
    unittest.main()
