"""Agent subprocess protocol. JSON commands on stdin, events on stdout.

The same worker runs locally or over an authenticated SSH connection. No
credentials are accepted in command-line arguments or written to configuration.
"""

from contextlib import redirect_stdout
import fcntl
import json
import logging
import os
from pathlib import Path
import signal
import sys
from threading import Event, Lock, Thread

from agent import Agent
from .redact import redact

PROTOCOL_VERSION = 2


def main() -> None:
    if sys.argv[1:] == ["--probe"]:
        print(json.dumps({"protocol": PROTOCOL_VERSION, "python": sys.version.split()[0]}))
        return
    output = sys.stdout
    output_lock = Lock()
    secret_values = []

    def emit(event):
        # Only structured, redacted events enter the stdout protocol.
        if event.get("type") == "ready":
            event = {**event, "pid": os.getpid()}
        raw = json.dumps(redact(event, secret_values), ensure_ascii=False)
        with output_lock:
            output.write(raw + "\n")
            output.flush()

    line = sys.stdin.readline(131073)
    if not line or len(line) > 131072:
        raise SystemExit("Missing or oversized worker configuration")
    command = json.loads(line)
    if command.get("type") != "start" or not isinstance(command.get("config"), dict):
        raise SystemExit("Expected start command")
    config = command["config"]
    secret_values.append(config.get("api_key", ""))
    workdir = Path(config["workdir"]).expanduser().resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    lockfile = (workdir / ".control.lock").open("a")
    try:
        fcntl.flock(lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        emit({"type": "error", "error": "This workspace already has a managed agent process"})
        raise SystemExit(1)
    failed = False
    finished = Event()
    try:
        # Console chunks may split a secret across writes. Suppress that raw
        # stream entirely; bounded completed events and formatted logs remain.
        with open(os.devnull, "w") as console, redirect_stdout(console), Agent(**config, event_sink=emit) as node:
            class RedactingFormatter(logging.Formatter):
                def format(self, record):
                    result = super().format(record)
                    return redact(result, secret_values)
            for handler in node.logger.handlers:
                handler.setFormatter(RedactingFormatter("%(asctime)s %(levelname)s %(message)s"))

            def heartbeat():
                while not finished.wait(2):
                    try:
                        emit({"type": "heartbeat", "state": node.state})
                    except (OSError, ValueError):
                        node.stop()
                        return

            def commands():
                try:
                    while True:
                        raw = sys.stdin.readline(131073)
                        if not raw:
                            break
                        if len(raw) > 131072:
                            raise ValueError("Control command is too large")
                        incoming = json.loads(raw)
                        if incoming.get("type") == "stop":
                            node.stop()
                            return
                        if incoming.get("type") == "delete_topic":
                            node.delete_topic(incoming["topic_id"])
                        if incoming.get("type") == "invite":
                            if incoming["secret"] not in secret_values:
                                secret_values.append(incoming["secret"])
                            node.invite_admin_topic(incoming["topic_id"], incoming["admin_id"], incoming["secret"])
                except Exception as exc:
                    emit({"type": "control_error", "error": str(exc)})
                finally:
                    # Loss of SSH/stdin requests a graceful remote shutdown.
                    node.stop()

            for sig in (signal.SIGTERM, signal.SIGINT):
                signal.signal(sig, lambda *_: node.stop())
            Thread(target=commands, name="agent-control-input", daemon=True).start()
            Thread(target=heartbeat, name="agent-control-heartbeat", daemon=True).start()
            node.run()
    except Exception as exc:
        failed = True
        emit({"type": "error", "error": f"{type(exc).__name__}: {exc}"})
    finally:
        finished.set()
        emit({"type": "worker_exit", "failed": failed})
        lockfile.close()
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
