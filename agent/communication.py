"""Construct the repository's real Comm backend; no local simulator fallback."""

import sys


def create_comm(config, *, seed=None):
    if sys.platform not in ("linux", "darwin") or sys.version_info < (3, 14):
        raise ValueError("comm requires Python 3.14+ on macOS/Linux; Windows is not supported")
    try:
        from comm import Comm
    except ImportError as error:
        raise ValueError("Cannot import comm; keep agent/ and comm/ together in the agent-group repository") from error
    seeds = tuple(dict.fromkeys((*config.comm_seeds, *((seed,) if seed else ()))))
    return Comm(host=config.comm_host, seeds=seeds, timeout=config.comm_timeout)
