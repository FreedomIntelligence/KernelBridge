"""Execute the shipped ACLNN macro on a deferred queue with lifetime checks."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

class AclnnLifetimeTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('c++'), 'C++ compiler required')
    def test_deferred_command_owns_operands_workspace_and_thread_local_state(self):
        header = (ROOT / 'source/packages/executor/src/kernelcat_executor/backends/ascend/templates/CppExtension/csrc/pytorch_npu_helper.hpp').read_text()
        macro = header[header.index('#define EXEC_NPU_CMD('):header.rindex('#endif')]
        fixture = (ROOT / 'validation/fixtures/aclnn_lifetime.cpp').read_text()
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'lifetime.cpp'
            binary = Path(directory) / 'lifetime'
            source.write_text(fixture.replace('PRODUCTION_MACRO', macro))
            subprocess.run(['c++', '-std=c++17', '-pthread', str(source), '-o', str(binary)], check=True, capture_output=True)
            subprocess.run([str(binary)], check=True, capture_output=True)
