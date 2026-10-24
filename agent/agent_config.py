"""Read shared model settings and per-agent settings from one .env file."""

import os
import re
import math
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values


@dataclass(frozen=True)
class AgentConfig:
    name: str
    initial_prompt: str
    workdir: Path
    topics: tuple[str, ...]


@dataclass(frozen=True)
class LaunchConfig:
    env_file: Path
    model: str
    base_url: str | None
    api_key: str = field(repr=False)
    comm_host: str | None
    comm_seeds: tuple[str, ...]
    comm_timeout: float
    startup_timeout: float
    topics: tuple[str, ...]
    agents: tuple[AgentConfig, ...]
    values: dict[str, str] = field(repr=False)

    def agent(self, name: str) -> AgentConfig:
        for entry in self.agents:
            if entry.name == name:
                return entry
        raise ValueError(f"Unknown agent: {name}")

    def environment(self, entry: AgentConfig) -> dict[str, str]:
        environment = {**os.environ, **self.values}
        environment.update({
            "AGENT_ENV_FILE": str(self.env_file),
            "AGENT_NAME": entry.name,
            "AGENT_WORKDIR": str(entry.workdir),
            "MODEL_ID": self.model,
            "ANTHROPIC_API_KEY": self.api_key,
        })
        if self.base_url:
            environment["ANTHROPIC_BASE_URL"] = self.base_url
        else:
            environment.pop("ANTHROPIC_BASE_URL", None)
        return environment


def split_list(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def load_config(path) -> LaunchConfig:
    env_file = Path(path).resolve()
    if not env_file.is_file():
        raise ValueError(f"Configuration file not found: {env_file}")
    values = {key: value for key, value in dotenv_values(env_file, encoding="utf-8").items()
              if value is not None}

    def value(key, default=""):
        return values.get(key, os.environ.get(key, default))

    def required(key):
        result = value(key).strip()
        if not result:
            raise ValueError(f"Missing configuration: {key}")
        return result

    def absolute(path_value):
        candidate = Path(path_value).expanduser()
        return (candidate if candidate.is_absolute() else env_file.parent / candidate).resolve()

    def positive_number(key, default):
        try:
            result = float(value(key, default))
        except ValueError:
            raise ValueError(f"{key} must be a positive number") from None
        if not math.isfinite(result) or result <= 0:
            raise ValueError(f"{key} must be a finite positive number")
        return result

    names = split_list(required("AGENTS"))
    if not names:
        raise ValueError("AGENTS must contain at least one agent")
    topics = split_list(value("TOPICS", "tasks,results"))
    if len(set(topics)) != len(topics):
        raise ValueError("TOPICS contains duplicate names")
    prefixes = set()
    directories = set()
    agents = []
    for name in names:
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", name):
            raise ValueError(f"Invalid agent name: {name}; use letters, digits, underscores or hyphens")
        prefix = "AGENT_" + name.upper().replace("-", "_")
        if prefix in prefixes:
            raise ValueError(f"Duplicate agent name or environment prefix: {name}")
        prefixes.add(prefix)
        workdir = absolute(required(prefix + "_WORKDIR"))
        directory_key = os.path.normcase(str(workdir))
        if directory_key in directories:
            raise ValueError(f"Agents must have distinct working directories: {workdir}")
        directories.add(directory_key)
        subscriptions = split_list(value(prefix + "_TOPICS"))
        if len(set(subscriptions)) != len(subscriptions):
            raise ValueError(f"Duplicate topic subscriptions for {name}")
        unknown = set(subscriptions) - set(topics)
        if unknown:
            raise ValueError(f"Unknown topics for {name}: {', '.join(sorted(unknown))}")
        agents.append(AgentConfig(name, required(prefix + "_INITIAL_PROMPT"), workdir, subscriptions))
    return LaunchConfig(
        env_file=env_file, model=required("MODEL_ID"),
        base_url=value("ANTHROPIC_BASE_URL").strip() or None,
        api_key=value("ANTHROPIC_API_KEY").strip(),
        comm_host=value("COMM_HOST").strip() or None,
        comm_seeds=split_list(value("COMM_SEEDS")),
        comm_timeout=positive_number("COMM_TIMEOUT", "5"),
        startup_timeout=positive_number("AGENT_STARTUP_TIMEOUT", "60"),
        topics=topics, agents=tuple(agents), values=values,
    )
