"""Check the effective Pi request against the configured delegation catalogue."""
import unittest

import test_runtime as runtime_support
from kernelcat_executor.capabilities.delegation import SubagentEffect


class DelegationGuidanceTests(unittest.TestCase):
    setUp = runtime_support.RuntimeTests.setUp
    tearDown = runtime_support.RuntimeTests.tearDown
    session = runtime_support.RuntimeTests.session

    def test_actual_request_uses_configured_roles_and_omits_unavailable_background(self):
        effect = SubagentEffect({'background': False, 'agents': [
            {'name': 'kernel-scout', 'description': 'Compare reference documents only', 'tools': 'read'},
            {'name': 'abi-auditor', 'description': 'Review the declared interface', 'tools': 'read'},
        ]})
        session, provider, replay = self.session([], extra_tools=[effect])
        self.assertEqual(session.run(self.task)['status'], 'finished')
        replay._boundary()
        effective = provider.harnesses[0]
        declaration = next(t for t in effective['tools'] if t['name'] == 'delegate')
        self.assertIn('Compare reference documents only', declaration['description'])
        self.assertIn('abi-auditor', declaration['parameters']['properties']['agent']['enum'])
        self.assertNotIn('background', declaration['parameters']['properties'])
        prompt = effective['system_prompt']
        self.assertIn('abi-auditor', prompt)
        self.assertIn('never infer permissions from a role name', prompt)
        self.assertNotIn('kernel-worker:', prompt)
        self.assertNotIn('background=true', prompt)

    def test_no_delegation_capability_does_not_advertise_child_launch(self):
        session, provider, replay = self.session([])
        self.assertEqual(session.run(self.task)['status'], 'finished')
        replay._boundary()
        effective = provider.harnesses[0]
        self.assertFalse(any(t['name'] == 'delegate' for t in effective['tools']))
        self.assertNotIn('delegate launches a real child agent', effective['system_prompt'])


if __name__ == '__main__':
    unittest.main()
