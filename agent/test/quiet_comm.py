"""No-discovery Comm replacement for tests of model requests only."""

import os
from unittest.mock import patch


def local_model_network():
    """Do not route loopback fixtures through a developer's HTTP proxy."""
    return patch.dict(os.environ, {"HTTP_PROXY": "", "HTTPS_PROXY": "", "ALL_PROXY": "",
                                   "http_proxy": "", "https_proxy": "", "all_proxy": "",
                                   "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"})


class QuietComm:
    def __init__(self, **_):
        self.agent_id = "model-test-agent"
        self.endpoint = "http://127.0.0.1:1"
        self._closed = False

    def close(self):
        self._closed = True
