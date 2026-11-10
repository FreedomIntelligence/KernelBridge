"""Expose configured finite business commands through the existing Gateway receipts."""
import hashlib
import os
from pathlib import Path
from jsonschema import Draft202012Validator
from kernelcat_executor.capabilities.process import CommandEvaluator
from kernelcat_executor.contracts.models import Outcome, ToolSpec

RESERVED = {'read', 'write', 'edit', 'bash', 'grep', 'find', 'ls', 'codemode', 'tool_search',
            'evaluate', 'read_file', 'write_file', 'edit_file', 'exec_command', 'finish', 'delegate',
            'read_receipt', 'run_experiment', 'replay', 'knowledge_search', 'knowledge_show',
            'knowledge_read', 'knowledge_source', 'load_skill', 'read_learned_skill'}


class PinnedCommand(CommandEvaluator):
    def execute(self, action):
        for pin in self.pins:
            path = Path(pin.path)
            if path.resolve() != path or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != pin.sha256:
                return Outcome(status='failed', data={'reason': 'business_tool_input_changed', 'unexecuted': True})
        return super().execute(action)


def configured_tools(configurations, workspace, state):
    names = set()
    result = []
    for item in configurations:
        if item.name in RESERVED or item.name in names:
            raise ValueError('Business tool name conflicts with an existing capability: ' + item.name)
        names.add(item.name)
        Draft202012Validator.check_schema(item.parameters)
        executable = Path(item.argv[0]) if item.argv else Path('')
        if not executable.is_absolute() or not os.access(executable, os.X_OK):
            raise ValueError('Business tool requires an absolute executable')
        for pin in item.files:
            path = Path(pin.path)
            if not path.is_absolute() or path.resolve() != path or path.is_relative_to(workspace.root):
                raise ValueError('Business tool pins must be canonical files outside its workspace')
            if hashlib.sha256(path.read_bytes()).hexdigest() != pin.sha256:
                raise ValueError('Business tool input digest changed: ' + str(path))
        tool = PinnedCommand(item.argv, workspace, item.parameters, item.credits, item.timeout,
            {key: os.environ[key] for key in item.env_allowlist if key in os.environ},
            artifact_root=state / 'business-tools' / item.name)
        tool.pins = item.files
        tool.spec = ToolSpec(name=item.name, description=item.description,
                            parameters=item.parameters, credits=item.credits)
        result.append(tool)
    return result
