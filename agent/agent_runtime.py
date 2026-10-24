"""The working/idle lifecycle, following the LCC s13 teammate runtime."""

import logging
import threading
from pathlib import Path


logger = logging.getLogger("agent")


class FileStopEvent:
    """Let a parent process request the usual graceful runtime shutdown."""

    def __init__(self, path):
        self.path = Path(path)
        self.event = threading.Event()

    def is_set(self):
        if self.path.exists():
            self.event.set()
        return self.event.is_set()

    def set(self):
        self.event.set()

    def wait(self, timeout=None):
        if self.is_set():
            return True
        self.event.wait(timeout)
        return self.is_set()


class AgentRuntime:
    def __init__(self, work, collect_work, stop_event=None, idle_interval: float = 0.2):
        if idle_interval <= 0:
            raise ValueError("idle_interval must be positive")
        self.work = work
        self.collect_work = collect_work
        self.stop_event = stop_event if stop_event is not None else threading.Event()
        self.idle_interval = idle_interval
        self.state = "idle"

    def set_state(self, state: str):
        if state != self.state:
            logger.info("Agent state: %s -> %s", self.state, state)
            self.state = state

    def wait_for_work(self) -> bool:
        while not self.stop_event.is_set():
            if self.collect_work():
                self.set_state("working")
                return True
            self.stop_event.wait(self.idle_interval)
        return False

    def run(self):
        self.set_state("working")
        try:
            while not self.stop_event.is_set():
                self.work()
                if self.stop_event.is_set():
                    break
                # Check before entering idle, so already queued events continue work.
                if not self.collect_work():
                    self.set_state("idle")
                    if not self.wait_for_work():
                        break
        finally:
            self.set_state("idle")
