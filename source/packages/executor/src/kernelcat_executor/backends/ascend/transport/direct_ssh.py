"""Optional direct OpenSSH transport with deployment-provided connection settings."""
import json
import os
from dataclasses import dataclass
from pathlib import Path
import subprocess


@dataclass(frozen=True)
class Config:
    host: str
    user: str
    port: int = 22
    identity: str | None = None

    @property
    def ssh_target(self):
        return self.user + '@' + self.host


def load_config(path=None):
    path = path or os.environ.get('KERNELBRIDGE_SSH_CONFIG')
    if not path:
        raise ValueError('Provide a direct SSH JSON configuration with host, user and port')
    values = json.loads(Path(path).read_text())
    config = Config(**values)
    if not config.host or not config.user or any(x.startswith('-') or any(c.isspace() for c in x) for x in (config.host, config.user)):
        raise ValueError('Invalid direct SSH target')
    if type(config.port) is not int or not 1 <= config.port <= 65535:
        raise ValueError('Invalid SSH port')
    return config


def ssh_argv(command=None, cfg=None):
    cfg = cfg or load_config()
    argv = ['ssh', '-F', os.devnull, '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
            '-o', 'ConnectTimeout=20', '-p', str(cfg.port)]
    if cfg.identity:
        argv += ['-i', cfg.identity]
    return [*argv, cfg.ssh_target, *([command] if command is not None else [])]


def ssh_exec(command, cfg=None, timeout=120, stdin=None, **_):
    return subprocess.run(ssh_argv(command, cfg), input=stdin, capture_output=True, timeout=timeout)


def ssh(command, cfg=None, timeout=120):
    result = ssh_exec(command, cfg, timeout)
    return subprocess.CompletedProcess(result.args, result.returncode,
        result.stdout.decode(errors='replace'), result.stderr.decode(errors='replace'))
