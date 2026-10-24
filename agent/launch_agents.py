"""Load .env and run each configured agent in its own process."""

import argparse
import importlib
import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
# Support both `python agent/launch_agents.py` and `python -m agent.launch_agents`.
# Local harness modules precede the repository root; comm is its sibling package.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT.parent) not in sys.path:
    sys.path.insert(1, str(ROOT.parent))

from agent_config import load_config
from agent_runtime import FileStopEvent
from communication import create_comm


def initialize_topics(config):
    endpoint = create_comm(config)
    try:
        discovered = endpoint.discover_topics()
        topics = {}
        for name in config.topics:
            matches = [topic for topic in discovered if topic.name == name]
            if len(matches) > 1:
                raise ValueError(f"Ambiguous topic name: {name}; choose a unique configured topic name")
            if matches:
                topics[name] = matches[0].topic_id
                endpoint.join_topic(topics[name])
            else:
                topics[name] = endpoint.create_topic(name, f"Configured topic: {name}")
        # Keep subscriptions alive until workers join. Comm topics have no owner
        # and disappear when the last member leaves.
        return endpoint, topics
    except BaseException:
        endpoint.close()
        raise


def run_worker(config, entry, stop_file):
    stop_file = Path(stop_file)
    startup = json.loads((stop_file.parent / "topics.json").read_text(encoding="utf-8"))
    os.environ.update(config.environment(entry))
    entry.workdir.mkdir(parents=True, exist_ok=True)
    os.chdir(entry.workdir)
    # Import only after both the environment and cwd identify this agent.
    agent = importlib.import_module(f"{__package__}.agent" if __package__ else "agent")
    endpoint = create_comm(config, seed=startup["seed"])
    agent.topic_manager = agent.TopicManager(endpoint)
    try:
        for name in entry.topics:
            agent.topic_manager.join_topic(startup["topics"][name])
        agent.logger.info("Agent ready: name=%s sender_id=%s endpoint=%s workdir=%s",
                          entry.name, endpoint.agent_id, endpoint.endpoint, entry.workdir)
        ready = stop_file.parent / "ready" / f"{entry.name}.json"
        ready.parent.mkdir(parents=True, exist_ok=True)
        temporary = ready.with_suffix(".tmp")
        temporary.write_text(json.dumps({
            "name": entry.name, "sender_id": endpoint.agent_id, "endpoint": endpoint.endpoint,
            "topics": {name: startup["topics"][name] for name in entry.topics},
        }, ensure_ascii=False), encoding="utf-8")
        temporary.replace(ready)
        stop = FileStopEvent(stop_file)
        deadline = time.monotonic() + config.startup_timeout
        # No model calls until every selected worker has subscribed. This avoids
        # losing startup messages sent before the other workers joined.
        while not (stop_file.parent / "START").exists():
            if stop.wait(0.1):
                return
            if time.monotonic() >= deadline:
                raise RuntimeError("Timed out waiting for the group startup barrier")
        if not stop.is_set():
            agent.run_agent(initial_prompt=entry.initial_prompt, stop_event=stop)
    finally:
        try:
            endpoint.close()
        finally:
            agent.topic_manager.joined_topics.clear()


def launch(config, selected):
    if not config.api_key:
        raise ValueError("请在 .env 中填写 ANTHROPIC_API_KEY；可先使用 --check 检查配置")
    if not selected:
        raise ValueError("Select at least one agent")
    stamp = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d_%H%M%S_%f")
    run_dir = config.env_file.parent / ".logs" / "launcher" / f"{stamp}_{os.getpid()}"
    run_dir.mkdir(parents=True, exist_ok=True)
    stop_file = run_dir / "STOP"
    logger = logging.getLogger("launcher")
    logger.setLevel(logging.INFO)
    handler = logging.FileHandler(run_dir / "launcher.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    processes = []
    outputs = []
    code = 0
    bootstrap = None
    started = False
    try:
        bootstrap, topics = initialize_topics(config)
        (run_dir / "topics.json").write_text(json.dumps({
            "seed": bootstrap.endpoint, "topics": topics,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        deadline = time.monotonic() + config.startup_timeout
        for entry in selected:
            logs = entry.workdir / ".logs"
            logs.mkdir(parents=True, exist_ok=True)
            output = (logs / "console.log").open("a", encoding="utf-8")
            outputs.append(output)
            process = subprocess.Popen(
                [sys.executable, "-X", "utf8", "-u", str(Path(__file__).resolve()),
                 "--env", str(config.env_file), "--worker", entry.name, "--stop-file", str(stop_file)],
                cwd=entry.workdir, env=config.environment(entry), stdout=output, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            processes.append((entry, process))
            logger.info("Started name=%s pid=%s workdir=%s", entry.name, process.pid, entry.workdir)
        while True:
            if not started and all((run_dir / "ready" / f"{entry.name}.json").is_file()
                                   for entry, _ in processes):
                bootstrap.close()
                bootstrap = None
                (run_dir / "START").write_text("start", encoding="utf-8")
                started = True
                logger.info("All agents subscribed; bootstrap closed and group started")
            exited = [(entry, process) for entry, process in processes if process.poll() is not None]
            if exited:
                for entry, process in exited:
                    logger.info("Exited name=%s code=%s", entry.name, process.returncode)
                    if process.returncode or not started:
                        code = 1
                break
            if not started and time.monotonic() >= deadline:
                logger.error("Timed out waiting for agent subscriptions")
                code = 1
                break
            time.sleep(0.2)
    except KeyboardInterrupt:
        logger.info("Stopping configured agents")
    finally:
        stop_file.write_text("stop", encoding="utf-8")
        if bootstrap is not None:
            bootstrap.close()
        for entry, process in processes:
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                logger.warning("Terminating name=%s after shutdown timeout", entry.name)
                process.terminate()
                process.wait(timeout=3)
        for output in outputs:
            output.close()
        logger.removeHandler(handler)
        handler.close()
    return code


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", type=Path, default=ROOT / ".env", help="Configuration file (default: .env)")
    parser.add_argument("--agent", help="Run only the named agent; otherwise run all configured agents")
    parser.add_argument("--check", action="store_true", help="Validate and show settings without starting agents")
    parser.add_argument("--worker", help=argparse.SUPPRESS)
    parser.add_argument("--stop-file", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        config = load_config(args.env)
        selected = (config.agent(args.agent),) if args.agent else config.agents
        if args.check:
            print(json.dumps({
                "env_file": str(config.env_file), "model": config.model, "base_url": config.base_url,
                "api_key_configured": bool(config.api_key),
                "communication": {"backend": "comm.Comm", "host": config.comm_host,
                                  "seeds": config.comm_seeds, "timeout": config.comm_timeout,
                                  "startup_timeout": config.startup_timeout},
                "agents": [{"name": entry.name, "workdir": str(entry.workdir),
                            "prompt_characters": len(entry.initial_prompt), "topics": entry.topics}
                           for entry in selected],
            }, ensure_ascii=False, indent=2))
            return 0
        if args.worker:
            if args.stop_file is None:
                raise ValueError("Worker requires --stop-file")
            run_worker(config, config.agent(args.worker), args.stop_file)
            return 0
        return launch(config, selected)
    except (ValueError, OSError, RuntimeError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    raise SystemExit(main())
