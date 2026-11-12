"""run_command subprocesses carry the W3C traceparent of their action's exported span."""
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
for path in [*ROOT.glob("source/packages/*/src"), ROOT / "source/apps/worker/src"]:
    sys.path.insert(0, str(path))

from kernelcat_executor.capabilities.commands import run_command_tool, traceparent
from kernelcat_executor.contracts.models import Action

EXPECTED = "00-8c5625cd1d97ac5943b189ec2bbc5a6f-d85f241c1b4c5055-01"  # also asserted by otlp-export.mjs


class TraceparentTests(unittest.TestCase):
    def action(self):
        return Action(id="task/step/0/model/primary/call/0", tool="run_command", task="task",
                      arguments={"command": 'printf %s "${TRACEPARENT:-none}"'})

    def test_same_ids_as_the_exporter(self):
        self.assertEqual(traceparent("application-test", "task/step/0/model/primary/call/0"), EXPECTED)

    def test_commands_receive_it_only_inside_an_application_session(self):
        with tempfile.TemporaryDirectory() as root:
            with_session = run_command_tool(Path(root), trace_session=lambda: "application-test").execute(self.action())
            without = run_command_tool(Path(root)).execute(self.action())
        self.assertEqual(with_session.data["output"].strip(), EXPECTED)
        self.assertEqual(without.data["output"].strip(), "none")


if __name__ == "__main__":
    unittest.main()
