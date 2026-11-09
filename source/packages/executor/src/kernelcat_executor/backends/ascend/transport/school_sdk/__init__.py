"""School-net access SDK: keep Clash on 7897, SSH demo2 via SLAI SOCKS5."""

from .config import Config, load_config
from .keep import KeepStatus, start_keep, stop_keep
from .proxy_win import apply_system_proxy, read_system_proxy, strip_ten_star
from .ssh import ssh

__all__ = [
    "Config",
    "KeepStatus",
    "apply_system_proxy",
    "load_config",
    "read_system_proxy",
    "ssh",
    "start_keep",
    "stop_keep",
    "strip_ten_star",
]
