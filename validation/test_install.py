"""Reject known incompatible Node before reserving an installation."""
import importlib.util
import json
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('bundle_install', Path(__file__).resolve().parents[1] / 'install.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class NodeAdmission(unittest.TestCase):
    def test_newer_release_missing_required_api_is_rejected(self):
        with patch.object(installer.subprocess, 'check_output', return_value=json.dumps({'version':'23.7.0','zstd':False})):
            with self.assertRaisesRegex(ValueError, 'Zstd'):
                installer.verify_node(Path('/fixture/node'), {})

    def test_supported_lts_is_accepted(self):
        with patch.object(installer.subprocess, 'check_output', return_value=json.dumps({'version':'22.22.3','zstd':True})):
            installer.verify_node(Path('/fixture/node'), {})
