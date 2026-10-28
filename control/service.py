"""Durable profiles, process supervision, and administrator-topic broadcasts."""

from collections import deque
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shlex
import subprocess
import sys
from threading import Event, Lock, RLock, Thread
import time
from urllib.parse import urlsplit
from uuid import uuid4

from comm import Comm
from agent.identity import profile_user_id, validate_user_id
from .redact import redact
from .history import TopicHistory, event_references_topic
from .cleanup import CleanupBlocked, locked_workspace, remove_profile_files, scrub_event_logs


class ControlError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def required_text(data, key, default=None, limit=1000):
    value = data.get(key, default)
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ControlError(f"{key} must be non-empty text (maximum {limit} characters)")
    return value.strip()


def ssh_command(host, *, probe=False):
    """One shell boundary, with every remote argument POSIX-quoted."""
    remote = "cd " + shlex.quote(host["repo_path"]) + " && exec " + shlex.quote(host["python_path"])
    remote += " -u -m control.worker" + (" --probe" if probe else "")
    return ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
            "-o", "StrictHostKeyChecking=yes",
            "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
            "-p", str(host["port"]), host["ssh_target"], remote]


class ControlService:
    def __init__(self, data_dir, *, comm_host=None, background=True):
        self.root = Path(data_dir).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.repo = Path(__file__).resolve().parents[1]
        self.lock = RLock()
        self.publish_lock = Lock()
        self.closed = Event()
        self.path = self.root / "control.json"
        self.lease = (self.root / ".server.lock").open("a")
        try:
            fcntl.flock(self.lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lease.close()
            raise ControlError("This data directory is already managed by another control server", 409) from None
        self.api_key = ""
        self.provider_keys = {}
        self.session_secrets = set()
        self._legacy_key_pending = ""
        self.csrf_token = secrets.token_urlsafe(32)
        self.admin_secret = secrets.token_urlsafe(48)
        self.processes = {}
        self.runtime = {}
        self.events = deque(maxlen=400)
        self.topic_cache = []
        self.last_attempt = {}
        self.last_invite = {}
        self.last_delete_attempt = {}
        self.last_cleanup_attempt = {}
        self.data = {"version": 1, "profiles": [], "hosts": [], "notices": [], "admin_topic": None,
                     "deleted_topics": [], "deleted_profile_ids": [], "cleanup_jobs": [], "model_providers": []}
        try:
            if self.path.exists():
                loaded = json.loads(self.path.read_text())
                if loaded.get("version") != 1:
                    raise ControlError("Unsupported control data version")
                self.data.update(loaded)
            identities = set()
            for profile in self.data["profiles"]:
                user_id = validate_user_id(profile.get("user_id", profile_user_id(profile["id"])))
                if user_id in identities:
                    raise ControlError("Agent profiles must have distinct user_id values")
                identities.add(user_id)
                profile["user_id"] = user_id
            self._migrate_model_providers()
        except BaseException:
            self.lease.close()
            raise
        if not any(host["id"] == "local" for host in self.data["hosts"]):
            self.data["hosts"].insert(0, {"id": "local", "name": "本机", "kind": "local"})
        self.comm_host = comm_host
        try:
            self.comm = Comm(host=comm_host, timeout=1.0)
        except BaseException:
            self.lease.close()
            raise
        self.topic = None
        self.history = None
        self.history_recovery = {}
        try:
            self.history = TopicHistory(self.root / "history.sqlite3")
            # Only explicit deletion intents are resumed. Older topic-retirement
            # records did not authorize purging history on upgrade.
            self._retry_cleanup_jobs(force=True)
            self.history.import_legacy(self.root, self.data["notices"])
            from .history_recovery import recover_truncated_sends
            recovered, self.history_recovery = recover_truncated_sends(
                self.history.truncated_sent_events(), self.root / "workspaces")
            for event in recovered:
                self.history.append_event(redact(event, self._secret_values()))
            if self.data["admin_topic"]:
                info = self.data["admin_topic"]
                topic_id = self.comm.create_topic(info["name"], info["description"])
                self.topic = {**info, "topic_id": topic_id}
                self.history.remember_topics([self.topic])
            for profile in self.data["profiles"]:
                self.runtime[profile["id"]] = self._empty_runtime()
            self.events.extend(self._event_preview(e) for e in self.history.recent_events())
            self._save()
        except BaseException:
            self.comm.close()
            if self.history is not None:
                self.history.close()
            self.lease.close()
            raise
        self.thread = None
        if background:
            self.thread = Thread(target=self._maintain, name="control-reconcile", daemon=True)
            self.thread.start()

    @staticmethod
    def _empty_runtime():
        return {"state": "stopped", "pid": None, "agent_id": None, "endpoint": None,
                "run_id": None, "admin_joined": False, "joined_topics": [], "error": None,
                "last_seen": None}

    def _save(self):
        # Caller holds self.lock once concurrent request handling begins.
        temporary = self.path.with_suffix(".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(self.data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.path)

    def _profile(self, identifier):
        found = next((p for p in self.data["profiles"] if p["id"] == identifier), None)
        if found is None:
            raise ControlError("Agent profile not found", 404)
        return found

    def _host(self, identifier):
        found = next((h for h in self.data["hosts"] if h["id"] == identifier), None)
        if found is None:
            raise ControlError("Host not found", 404)
        return found

    def _secret_values(self):
        return (*self.session_secrets, self.api_key, self.admin_secret)

    @staticmethod
    def _validate_provider(data, identifier):
        base_url = required_text(data, "base_url", limit=2048).rstrip("/")
        try:
            url = urlsplit(base_url)
            url.port  # Validate malformed or out-of-range explicit ports too.
        except ValueError:
            raise ControlError("base_url must be a valid HTTP(S) URL") from None
        if (url.scheme not in ("http", "https") or not url.hostname or url.username or url.password
                or url.query or url.fragment or any(c.isspace() for c in base_url)):
            raise ControlError("base_url must be an HTTP(S) URL without credentials, query or fragment")
        mode = data.get("api_mode", "openai")
        if mode not in ("openai", "deepseek"):
            raise ControlError("api_mode must be openai or deepseek")
        return {"id": identifier, "name": required_text(data, "name", limit=100),
                "base_url": base_url, "model": required_text(data, "model", limit=150), "api_mode": mode}

    def _provider(self, identifier):
        provider = next((p for p in self.data["model_providers"] if p["id"] == identifier), None)
        if provider is None:
            raise ControlError("Model provider not found", 404)
        return provider

    def _public_provider(self, provider):
        return {**provider, "credentials_configured": bool(self.provider_keys.get(provider["id"]))}

    def _resolved_profile(self, profile):
        provider = next((p for p in self.data["model_providers"] if p["id"] == profile.get("provider_id")), None)
        return {**profile, "provider_name": provider["name"] if provider else "",
                "base_url": provider["base_url"] if provider else "", "model": provider["model"] if provider else "",
                "api_mode": provider["api_mode"] if provider else "openai", "model_configured": provider is not None,
                "credentials_configured": bool(provider and self.provider_keys.get(provider["id"]))}

    def _legacy_provider(self, profile):
        base_url = required_text(profile, "base_url", "https://api.deepseek.com", limit=2048).rstrip("/")
        mode = profile.get("api_mode") or ("deepseek" if urlsplit(base_url).hostname == "api.deepseek.com" else "openai")
        model = required_text(profile, "model", "deepseek-flash", limit=150)
        value = self._validate_provider({"name": "DeepSeek" if mode == "deepseek" else "OpenAI 兼容接口",
                                         "base_url": base_url, "model": model, "api_mode": mode}, uuid4().hex)
        existing = next((p for p in self.data["model_providers"] if all(
            p[key] == value[key] for key in ("base_url", "model", "api_mode"))), None)
        if existing:
            return existing
        self.data["model_providers"].append(value)
        return value

    def _migrate_model_providers(self):
        # Old profiles carried their own endpoint/model. Preserve that pairing;
        # never infer one global key for several independently configured hosts.
        self.data["model_providers"] = [self._validate_provider(p, p["id"]) for p in self.data["model_providers"]]
        for profile in self.data["profiles"]:
            if not profile.get("provider_id"):
                profile["provider_id"] = self._legacy_provider(profile)["id"]
            for field in ("base_url", "model", "api_mode", "api_key"):
                profile.pop(field, None)

    def save_model_provider(self, data, identifier=None):
        with self.lock:
            old = self._provider(identifier) if identifier else None
            provider = self._validate_provider(data, identifier or uuid4().hex)
            if "clear_api_key" in data and type(data["clear_api_key"]) is not bool:
                raise ControlError("clear_api_key must be a boolean")
            key = data.get("api_key", "")
            if not isinstance(key, str) or len(key) > 4096:
                raise ControlError("api_key must be text (maximum 4096 characters)")
            key = key.strip()
            if key and data.get("clear_api_key"):
                raise ControlError("Cannot replace and clear api_key in the same request")
            previous = self.data["model_providers"]
            self.data["model_providers"] = ([provider if p["id"] == identifier else p for p in previous]
                                            if old else [*previous, provider])
            try:
                self._save()
            except BaseException:
                self.data["model_providers"] = previous
                raise
            self._legacy_key_pending = ""
            # Changing the destination must not silently forward an old secret.
            if data.get("clear_api_key") or (old and old["base_url"] != provider["base_url"]):
                self.provider_keys.pop(provider["id"], None)
            if key:
                self.provider_keys[provider["id"]] = key
                self.session_secrets.add(key)
            return self._public_provider(provider)

    def delete_model_provider(self, identifier):
        with self.lock:
            self._provider(identifier)
            if any(p.get("provider_id") == identifier for p in self.data["profiles"]):
                raise ControlError("Model provider is used by agents; change their model configuration first", 409)
            previous = self.data["model_providers"]
            self.data["model_providers"] = [p for p in previous if p["id"] != identifier]
            try:
                self._save()
            except BaseException:
                self.data["model_providers"] = previous
                raise
            self.provider_keys.pop(identifier, None)
            return {"deleted": identifier}

    def set_credentials(self, data):
        key = required_text(data, "api_key", limit=4096)
        with self.lock:
            provider_id = data.get("provider_id")
            if provider_id:
                self._provider(provider_id)
            elif len(self.data["model_providers"]) == 1:
                provider_id = self.data["model_providers"][0]["id"]
            elif self.data["model_providers"]:
                raise ControlError("Select provider_id when configuring credentials for multiple providers")
            if provider_id:
                self.provider_keys[provider_id] = key
                self._legacy_key_pending = ""
            else:
                # Compatibility for old clients setting a key before creating
                # their first legacy profile. Explicit providers never inherit it.
                self._legacy_key_pending = key
            self.api_key = key
            self.session_secrets.add(key)
        return {"credentials_configured": True}

    def save_profile(self, data, identifier=None):
        if not isinstance(data, dict):
            raise ControlError("Profile must be an object")
        with self.lock:
            if identifier:
                self._profile(identifier)
                if self._cleanup_job("profile", identifier):
                    raise ControlError("Agent deletion is pending; retry cleanup before editing", 409)
                proc = self.processes.get(identifier)
                if proc and proc["process"].poll() is None:
                    raise ControlError("Stop the agent before editing its profile", 409)
            name = required_text(data, "name", limit=80)
            if name in (".", "..") or any(c in name for c in ("/", "\\", "\x00")):
                raise ControlError("Invalid agent name")
            if any(p["name"] == name and p["id"] != identifier for p in self.data["profiles"]):
                raise ControlError("Agent name already exists", 409)
            persona = required_text(data, "persona", limit=16000)
            initial = required_text(data, "initial_prompt", "完成当前任务后等待 topic 消息。", limit=16000)
            host_id = required_text(data, "host_id", "local")
            self._host(host_id)
            for key in ("enable_memory", "thinking"):
                if key in data and type(data[key]) is not bool:
                    raise ControlError(f"{key} must be a boolean")
            profile_id = identifier or uuid4().hex
            previous = self._profile(identifier) if identifier else None
            user_id = previous.get("user_id", profile_user_id(profile_id)) if previous else profile_user_id(profile_id)
            if "user_id" in data and data["user_id"] != user_id:
                raise ControlError("user_id is generated by the platform and cannot be changed")
            previous_data = deepcopy(self.data)
            legacy = False
            if data.get("provider_id"):
                provider = self._provider(data["provider_id"])
            elif "base_url" in data or "model" in data:
                required_text(data, "base_url", limit=2048)
                required_text(data, "model", limit=150)
                provider = self._legacy_provider(data)
                legacy = True
            elif previous and previous.get("provider_id"):
                provider = self._provider(previous["provider_id"])
            elif len(self.data["model_providers"]) == 1:
                provider = self.data["model_providers"][0]
            else:
                raise ControlError("Select a model provider before creating an agent")
            profile = {"id": profile_id, "user_id": user_id, "name": name, "persona": persona,
                       "initial_prompt": initial, "provider_id": provider["id"],
                       "host_id": host_id, "enable_memory": data.get("enable_memory", False),
                       "thinking": data.get("thinking", False)}
            if identifier:
                self.data["profiles"] = [profile if p["id"] == identifier else p for p in self.data["profiles"]]
            else:
                self.data["profiles"].append(profile)
                self.runtime[profile["id"]] = self._empty_runtime()
            try:
                self._save()
            except BaseException:
                self.data = previous_data
                if not identifier:
                    self.runtime.pop(profile_id, None)
                raise
            if legacy and self._legacy_key_pending:
                self.provider_keys[provider["id"]] = self._legacy_key_pending
                self._legacy_key_pending = ""
            return self._resolved_profile(profile)

    def delete_profile(self, identifier):
        # poll() can report exit while the reader still owns its log and waits
        # for self.lock. Drain it outside the lock, then recheck under the lock.
        with self.lock:
            self._profile(identifier)
            record = self.processes.get(identifier)
            if record and record["process"].poll() is None:
                raise ControlError("Stop the agent before deleting its profile", 409)
            reader = record.get("reader") if record else None
        if reader:
            reader.join(timeout=3)
            if reader.is_alive():
                raise ControlError("Agent output is still draining; retry deletion shortly", 409)
        with self.publish_lock, self.lock:
            profile = self._profile(identifier)
            self._check_profile_cleanup(identifier)
            job = self._cleanup_job("profile", identifier)
            if job is None:
                # Detect an orphaned local worker before accepting deletion.
                try:
                    with locked_workspace(self.root, identifier):
                        pass
                except (CleanupBlocked, OSError) as exc:
                    raise ControlError(str(exc), 409) from exc
                job = {"kind": "profile", "id": identifier, "name": profile["name"], "created_at": timestamp()}
                self.data["cleanup_jobs"].append(job)
                try:
                    self._save()
                except BaseException:
                    self.data["cleanup_jobs"].remove(job)
                    raise
            self._attempt_cleanup(job)
            return {"deleted": identifier, **self._cleanup_state("profile", identifier)}

    def _cleanup_job(self, kind, identifier):
        return next((job for job in self.data["cleanup_jobs"]
                     if job["kind"] == kind and job["id"] == identifier), None)

    def _cleanup_state(self, kind, identifier):
        job = self._cleanup_job(kind, identifier)
        return {"cleanup_pending": job is not None, "cleanup_error": job.get("error") if job else None,
                "history_revision": self.history.revision()}

    def _check_profile_cleanup(self, identifier):
        profile = self._profile(identifier)
        record = self.processes.get(identifier)
        if record and record["process"].poll() is None:
            raise ControlError("Stop the agent before deleting its profile", 409)
        if record and (reader := record.get("reader")) and reader.is_alive():
            raise ControlError("Agent output is still draining; retry deletion shortly", 409)
        # An SSH transport exit cannot prove the remote worker or workspace is
        # gone. Until remote cleanup is verified, retain its configuration.
        if self._host(profile["host_id"])["kind"] == "ssh" or (record and record.get("host_kind") == "ssh"):
            raise ControlError("This version cannot verify remote workspace cleanup; the SSH agent configuration has been retained", 409)

    def _attempt_cleanup(self, job):
        """Complete an already-persisted intent, or leave a retryable journal.

        Caller holds publish_lock then self.lock. A committed SQLite tombstone
        makes partial file/config failures safe against late-event resurrection.
        """
        kind, identifier = job["kind"], job["id"]
        self.last_cleanup_attempt[(kind, identifier)] = time.monotonic()
        previous = deepcopy(self.data)
        try:
            if kind == "profile":
                self._check_profile_cleanup(identifier)
                with locked_workspace(self.root, identifier) as workspace:
                    self.history.delete_profile(identifier)
                    self.events = deque((e for e in self.events if e.get("profile_id") != identifier), maxlen=400)
                    scrub_event_logs(self.root, lambda e: e.get("profile_id") == identifier)
                    remove_profile_files(self.root, identifier, workspace)
                self.data["profiles"] = [p for p in self.data["profiles"] if p["id"] != identifier]
                if identifier not in self.data["deleted_profile_ids"]:
                    self.data["deleted_profile_ids"].append(identifier)
                for notice in self.data["notices"]:
                    notice["targets"] = [t for t in notice["targets"] if t["profile_id"] != identifier]
                    notice["receipts"].pop(identifier, None)
            elif kind == "topic":
                self.history.delete_topic(identifier)
                self.events = deque((e for e in self.events if not event_references_topic(e, identifier)), maxlen=400)
                scrub_event_logs(self.root, lambda e: event_references_topic(e, identifier))
                self.comm.leave_topic(identifier)
                self.topic_cache = [t for t in self.topic_cache if t["topic_id"] != identifier]
                for runtime in self.runtime.values():
                    runtime["joined_topics"] = [t for t in runtime["joined_topics"] if t != identifier]
                self.data["notices"] = [n for n in self.data["notices"] if n.get("topic_id") != identifier]
            else:
                raise ValueError("Unknown cleanup job kind")
            self.data["cleanup_jobs"] = [j for j in self.data["cleanup_jobs"]
                                         if (j["kind"], j["id"]) != (kind, identifier)]
            self._save()
        except Exception as exc:
            self.data = previous
            pending = self._cleanup_job(kind, identifier)
            pending["error"] = str(redact(str(exc), self._secret_values()))
            pending["last_attempt_at"] = timestamp()
            try:
                self._save()
            except Exception:
                # The original intent was already committed. It will retry on
                # restart even when recording the newest error also fails.
                pass
            return
        if kind == "profile":
            self.runtime.pop(identifier, None)
            record = self.processes.pop(identifier, None)
            if record:
                self.last_delete_attempt = {key: value for key, value in self.last_delete_attempt.items()
                                            if key[0] != record.get("run_id")}
            self.last_invite.pop(identifier, None)
        self.last_cleanup_attempt.pop((kind, identifier), None)
        valid_notices = {n["id"] for n in self.data["notices"]}
        self.last_attempt = {key: value for key, value in self.last_attempt.items() if key in valid_notices}

    def _retry_cleanup_jobs(self, *, force=False):
        with self.publish_lock, self.lock:
            for job in list(self.data["cleanup_jobs"]):
                key = (job["kind"], job["id"])
                if force or time.monotonic() - self.last_cleanup_attempt.get(key, -10) >= 5:
                    self._attempt_cleanup(job)

    def add_host(self, data):
        if data.get("kind", "ssh") != "ssh":
            raise ControlError("The built-in local host already exists")
        target = required_text(data, "ssh_target", limit=255)
        if target.startswith("-") or not re.fullmatch(r"[a-zA-Z0-9_@.\-:\[\]]+", target):
            raise ControlError("Use an SSH host alias or user@hostname, without command options")
        repo = required_text(data, "repo_path", limit=2048)
        workspace = required_text(data, "workspace_root", limit=2048)
        if not PurePosixPath(repo).is_absolute() or not PurePosixPath(workspace).is_absolute():
            raise ControlError("Remote repository and workspace paths must be absolute")
        port = data.get("port", 22)
        if type(port) is not int or not 1 <= port <= 65535:
            raise ControlError("SSH port must be an integer from 1 to 65535")
        python = required_text(data, "python_path", "python3", limit=2048)
        if "\x00" in repo + workspace + python:
            raise ControlError("Invalid remote path")
        host = {"id": uuid4().hex, "name": required_text(data, "name", limit=100), "kind": "ssh",
                "ssh_target": target, "port": port, "repo_path": repo, "python_path": python,
                "workspace_root": workspace, "comm_host": data.get("comm_host") or None}
        if host["comm_host"] is not None and not isinstance(host["comm_host"], str):
            raise ControlError("comm_host must be a string")
        with self.lock:
            self.data["hosts"].append(host)
            self._save()
        return deepcopy(host)

    def check_host(self, identifier):
        with self.lock:
            host = deepcopy(self._host(identifier))
        command = ([sys.executable, "-m", "control.worker", "--probe"]
                   if host["kind"] == "local" else ssh_command(host, probe=True))
        try:
            result = subprocess.run(command, cwd=self.repo, text=True, capture_output=True, timeout=15)
            payload = json.loads(result.stdout.strip().splitlines()[-1]) if result.stdout.strip() else {}
            if not isinstance(payload, dict):
                payload = {}
            ok = result.returncode == 0 and payload.get("protocol") == 2
            error = None if ok else (result.stderr[-1500:] or "Worker protocol mismatch; update the remote checkout")
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            ok, error, payload = False, str(exc), {}
        check = {"ok": ok, "checked_at": timestamp(), "python": payload.get("python"), "error": error}
        with self.lock:
            self._host(identifier)["last_check"] = check
            self._save()
        return check

    def _write_command(self, record, command):
        with record["write_lock"]:
            record["process"].stdin.write(json.dumps(command, ensure_ascii=False) + "\n")
            record["process"].stdin.flush()

    def start_profile(self, identifier):
        with self.lock:
            if self.closed.is_set():
                raise ControlError("Control service is shutting down", 409)
            profile = deepcopy(self._profile(identifier))
            if self._cleanup_job("profile", identifier):
                raise ControlError("Agent deletion is pending; retry cleanup before starting", 409)
            existing = self.processes.get(identifier)
            if existing and existing["process"].poll() is None:
                raise ControlError("Agent is already running or stopping", 409)
            provider = self._provider(profile["provider_id"])
            api_key = self.provider_keys.get(provider["id"])
            if not api_key:
                raise ControlError(f"Set the session API key for model provider {provider['name']} before starting agents", 409)
            host = deepcopy(self._host(profile["host_id"]))
            run_id = uuid4().hex
            workdir = (str(self.root / "workspaces" / identifier) if host["kind"] == "local"
                       else str(PurePosixPath(host["workspace_root"]) / identifier))
            config = {key: profile[key] for key in ("name", "persona", "initial_prompt",
                                                     "thinking", "enable_memory", "user_id")}
            config.update({key: provider[key] for key in ("base_url", "model", "api_mode")})
            config.update(api_key=api_key, profile_id=identifier, workdir=workdir,
                          host=self.comm_host if host["kind"] == "local" else host.get("comm_host"),
                          seeds=[self.comm.endpoint],
                          deleted_topic_ids=[t["topic_id"] for t in self.data["deleted_topics"]])
            command = ([sys.executable, "-u", "-m", "control.worker"] if host["kind"] == "local"
                       else ssh_command(host))
            logs = self.root / "logs"
            logs.mkdir(exist_ok=True, mode=0o700)
            log = open(logs / f"{identifier}-{run_id}.log", "a", encoding="utf-8")
            try:
                process = subprocess.Popen(command, cwd=self.repo, stdin=subprocess.PIPE,
                                           stdout=subprocess.PIPE, stderr=log, text=True, bufsize=1)
            except OSError as exc:
                log.close()
                raise ControlError(f"Could not start worker: {exc}", 502) from exc
            record = {"process": process, "write_lock": Lock(), "run_id": run_id, "log": log,
                      "host_kind": host["kind"], "worker_exit": False,
                      "deleted_topic_ids": set(), "topic_delete_errors": {}}
            self.processes[identifier] = record
            self.runtime[identifier] = {**self._empty_runtime(), "state": "starting",
                                        "run_id": run_id, "pid": process.pid if host["kind"] == "local" else None,
                                        "control_pid": process.pid}
            try:
                self._write_command(record, {"type": "start", "config": config})
            except (OSError, ValueError) as exc:
                process.terminate()
                process.wait(timeout=5)
                log.close()
                self.runtime[identifier].update(state="failed", error="Worker input closed before startup")
                raise ControlError("Worker input closed before startup", 502) from exc
            thread = Thread(target=self._read_worker, args=(identifier, record), daemon=True,
                            name=f"control-worker-{identifier[:8]}")
            record["reader"] = thread
            thread.start()
            return deepcopy(self.runtime[identifier])

    def stop_profile(self, identifier):
        with self.lock:
            self._profile(identifier)
            record = self.processes.get(identifier)
            if not record or record["process"].poll() is not None:
                return deepcopy(self.runtime[identifier])
            self.runtime[identifier]["state"] = "stopping"
            try:
                self._write_command(record, {"type": "stop"})
            except (OSError, ValueError):
                self.runtime[identifier]["error"] = "Control connection lost; shutdown is not confirmed"
            return deepcopy(self.runtime[identifier])

    @staticmethod
    def _event_preview(event):
        # Dashboard polling stays bounded. Complete message bodies are retained
        # in the durable archive and fetched by the paginated conversation API.
        preview = deepcopy(event)
        if isinstance(preview.get("payload"), str) and len(preview["payload"]) > 2000:
            preview["payload"] = preview["payload"][:2000]
            preview["truncated"] = True
            preview["preview_truncated"] = True
        return preview

    def _record_event(self, profile_id, run_id, event):
        deleted_profiles = set(self.data["deleted_profile_ids"])
        deleted_profiles.update(j["id"] for j in self.data["cleanup_jobs"] if j["kind"] == "profile")
        if profile_id in deleted_profiles or any(
                event_references_topic(event, topic["topic_id"]) for topic in self.data["deleted_topics"]):
            return
        # Protocol fields containing credentials are never retained or returned.
        event = {k: v for k, v in event.items() if k not in ("api_key", "secret", "signature", "reasoning_content")}
        event = redact(event, self._secret_values())
        if "name" in event:
            event["tool_name" if event.get("type", "").startswith("tool_") else "topic_name"] = event["name"]
        event.update(id=uuid4().hex, ts=timestamp(), profile_id=profile_id, run_id=run_id,
                     name=self._profile(profile_id)["name"] if profile_id else "管理员")
        self.history.append_event(event)
        self.events.append(self._event_preview(event))
        path = self.root / "events.jsonl"
        if path.exists() and path.stat().st_size > 8_000_000:
            os.replace(path, self.root / f"events.{time.time_ns()}.{uuid4().hex[:8]}.jsonl")
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")

    def handle_event(self, identifier, run_id, event):
        with self.lock:
            runtime = self.runtime.get(identifier)
            if not runtime or runtime["run_id"] != run_id or not isinstance(event, dict):
                return
            event = redact(event, self._secret_values())
            kind = event.get("type")
            if not isinstance(kind, str):
                return
            runtime["last_seen"] = timestamp()
            if kind == "heartbeat":
                if runtime["state"] not in ("stopping", "failed") and event.get("state") in ("idle", "working"):
                    runtime["state"] = event["state"]
                return
            if kind == "ready":
                runtime.update(agent_id=event.get("agent_id"), endpoint=event.get("endpoint"))
                runtime["user_id"] = event.get("user_id")
                if type(event.get("pid")) is int:
                    runtime["pid"] = event["pid"]
                if runtime["state"] == "starting":
                    runtime["state"] = "idle"
            elif kind == "state":
                value = event.get("state")
                if value in ("idle", "working", "stopped") and runtime["state"] != "stopping":
                    runtime["state"] = value
            elif kind in ("error", "control_error"):
                runtime.update(state="failed", error=str(event.get("error", "Worker failed")))
            elif kind == "worker_exit":
                if record := self.processes.get(identifier):
                    record["worker_exit"] = True
                runtime["state"] = "failed" if event.get("failed") or runtime["error"] else "stopped"
            elif kind == "admin_joined" and self.topic and event.get("topic_id") == self.topic["topic_id"]:
                runtime["admin_joined"] = True
                runtime.pop("invite_error", None)
            elif kind == "admin_join_failed":
                runtime["invite_error"] = event.get("error")
            elif kind == "topic_joined":
                deleted = {topic["topic_id"] for topic in self.data["deleted_topics"]}
                if event.get("topic_id") not in deleted and event.get("topic_id") not in runtime["joined_topics"]:
                    runtime["joined_topics"].append(event.get("topic_id"))
            elif kind in ("topic_left", "topic_deleted"):
                runtime["joined_topics"] = [t for t in runtime["joined_topics"] if t != event.get("topic_id")]
                if kind == "topic_deleted" and (record := self.processes.get(identifier)):
                    record.setdefault("deleted_topic_ids", set()).add(event.get("topic_id"))
                    record.setdefault("topic_delete_errors", {}).pop(event.get("topic_id"), None)
            elif kind == "topic_delete_failed":
                if record := self.processes.get(identifier):
                    record.setdefault("topic_delete_errors", {})[event.get("topic_id")] = event.get("error", "Delete failed")
            elif kind in ("notice_received", "notice_applied"):
                notice = next((n for n in self.data["notices"] if n["id"] == event.get("notice_id")), None)
                if notice and identifier in notice["receipts"]:
                    receipt = notice["receipts"][identifier]
                    receipt.setdefault("received_at", timestamp())
                    if kind == "notice_applied":
                        receipt.update(status="applied", applied_at=timestamp())
                    elif receipt["status"] != "applied":
                        receipt["status"] = "received"
                    self._save()
            self._record_event(identifier, run_id, event)

    def _read_worker(self, identifier, record):
        process = record["process"]
        try:
            while raw := process.stdout.readline(8 * 1_048_576 + 1):
                if len(raw) > 8 * 1_048_576:
                    raise ValueError("Oversized worker event")
                try:
                    event = json.loads(raw)
                except ValueError:
                    continue
                self.handle_event(identifier, record["run_id"], event)
            code = process.wait()
            with self.lock:
                runtime = self.runtime.get(identifier)
                if runtime and runtime["run_id"] == record["run_id"]:
                    if not record["worker_exit"]:
                        runtime.update(state="failed", error=(
                            "SSH connection ended without shutdown confirmation; remote process state is unknown"
                            if record["host_kind"] == "ssh" else f"Worker exited before handshake/cleanup (exit {code}); see run log"))
                    runtime["admin_joined"] = False
                    runtime["joined_topics"] = []
                    runtime["pid"] = None
        except Exception as exc:
            self.handle_event(identifier, record["run_id"], {"type": "error", "error": str(exc)})
        finally:
            for stream in (process.stdin, process.stdout, record["log"]):
                try:
                    stream.close()
                except OSError:
                    pass

    def create_admin_topic(self, data):
        name = required_text(data, "name", "管理员通知", limit=100)
        description = data.get("description", "由控制台管理的通知频道")
        if not isinstance(description, str) or len(description) > 1000:
            raise ControlError("Invalid topic description")
        with self.lock:
            if self.topic:
                raise ControlError("An administrator topic already exists", 409)
            topic_id = self.comm.create_topic(name, description)
            self.topic = {"topic_id": topic_id, "name": name, "description": description}
            self.data["admin_topic"] = {"name": name, "description": description}
            self._save()
            self.history.remember_topics([self.topic])
        self.invite_all()
        return deepcopy(self.topic)

    def _topic_deletion_state(self, topic):
        """Caller holds self.lock; ACKs describe actual managed-worker completion."""
        identifier = topic["topic_id"]
        pending, errors = [], {}
        for profile_id, record in self.processes.items():
            live = record["process"].poll() is None
            remote_unknown = (record.get("host_kind") == "ssh" and not record.get("worker_exit")
                              and self.runtime.get(profile_id, {}).get("state") == "failed")
            if (live or remote_unknown) and identifier not in record.get("deleted_topic_ids", ()):
                pending.append(profile_id)
                if not live and remote_unknown:
                    errors[profile_id] = "Remote worker shutdown is unconfirmed; reconnect before deletion can be confirmed"
                elif error := record.get("topic_delete_errors", {}).get(identifier):
                    errors[profile_id] = error
        return {**deepcopy(topic), "pending_profiles": pending, "errors": errors, "scope": "managed_agents"}

    def _dispatch_topic_deletions(self):
        """Retry on the authenticated local/SSH control channel, never over LAN discovery."""
        with self.lock:
            for topic in self.data["deleted_topics"]:
                topic_id = topic["topic_id"]
                for profile_id, record in self.processes.items():
                    if record["process"].poll() is not None or topic_id in record.get("deleted_topic_ids", ()):
                        continue
                    key = (record["run_id"], topic_id)
                    if time.monotonic() - self.last_delete_attempt.get(key, -10) < 5:
                        continue
                    self.last_delete_attempt[key] = time.monotonic()
                    try:
                        self._write_command(record, {"type": "delete_topic", "topic_id": topic_id})
                        record.setdefault("topic_delete_errors", {}).pop(topic_id, None)
                    except (OSError, ValueError):
                        record.setdefault("topic_delete_errors", {})[topic_id] = "Worker control connection is unavailable; will retry"

    def delete_topic(self, identifier):
        """Persist a deletion intent before purging records and retiring membership."""
        with self.publish_lock, self.lock:
            if self.topic and identifier == self.topic["topic_id"]:
                raise ControlError("当前管理员通知频道用于全体通知，不能删除", 409)
            existing = next((t for t in self.data["deleted_topics"] if t["topic_id"] == identifier), None)
            previous = deepcopy(self.data)
            if existing is None:
                metadata = next((t for t in self.topic_cache if t["topic_id"] == identifier), None) or self.history.topic(identifier)
                events = [e for e in self.events if event_references_topic(e, identifier)]
                known_member = any(identifier in r["joined_topics"] for r in self.runtime.values())
                if metadata is None and not events and not known_member:
                    raise ControlError("Topic not found", 404)
                name = (metadata or {}).get("name") or next(
                    (e["topic_name"] for e in reversed(events) if e.get("topic_name")), identifier[:12])
                existing = {"topic_id": identifier, "name": name, "deleted_at": timestamp()}
                self.data["deleted_topics"].append(existing)
            job = self._cleanup_job("topic", identifier)
            if job is None:
                job = {"kind": "topic", "id": identifier, "name": existing["name"], "created_at": timestamp()}
                self.data["cleanup_jobs"].append(job)
                try:
                    self._save()
                except BaseException:
                    self.data = previous
                    raise
            self._attempt_cleanup(job)
            self._dispatch_topic_deletions()
            return {"deleted": identifier, **self._topic_deletion_state(existing),
                    **self._cleanup_state("topic", identifier)}

    def invite_all(self):
        with self.lock:
            if not self.topic:
                raise ControlError("Create the administrator topic first", 409)
            invited = []
            for identifier, record in self.processes.items():
                runtime = self.runtime[identifier]
                if record["process"].poll() is not None or not runtime["agent_id"]:
                    continue
                if runtime["admin_joined"] or runtime["state"] in ("stopping", "stopped", "failed"):
                    continue
                try:
                    self._write_command(record, {"type": "invite", "topic_id": self.topic["topic_id"],
                                                 "admin_id": self.comm.agent_id, "secret": self.admin_secret})
                    self.last_invite[identifier] = time.monotonic()
                    invited.append(identifier)
                except (OSError, ValueError):
                    runtime["invite_error"] = "Worker control connection is unavailable"
            return {"invited": invited}

    def create_notice(self, data):
        content = required_text(data, "content", limit=12000)
        with self.lock:
            if not self.topic:
                raise ControlError("Create the administrator topic first", 409)
            profiles = [p for p in self.data["profiles"] if not self._cleanup_job("profile", p["id"])]
            if not profiles:
                raise ControlError("Create at least one agent profile first", 409)
            targets = [{"profile_id": p["id"], "name": p["name"], "host_id": p["host_id"]}
                       for p in profiles]
            notice = {"id": uuid4().hex, "content": content, "created_at": timestamp(),
                      "topic_id": self.topic["topic_id"], "targets": targets, "attempts": 0,
                      "publish_error": None, "receipts": {t["profile_id"]: {"status": "pending"} for t in targets}}
            self.data["notices"].append(notice)
            self._save()
            self.history.add_notice(notice)
        return self.publish_notice(notice["id"])

    def delete_notice(self, identifier):
        """Remove the notification-center record, without retracting delivery.

        Serialize with publishers so a captured retry cannot recreate or save
        the removed record after this operation returns. Already delivered or
        in-flight messages and archived topic history remain unchanged.
        """
        with self.publish_lock:
            with self.lock:
                previous = self.data["notices"]
                if not any(notice["id"] == identifier for notice in previous):
                    raise ControlError("Notice not found", 404)
                self.data["notices"] = [notice for notice in previous if notice["id"] != identifier]
                try:
                    self._save()
                except BaseException:
                    self.data["notices"] = previous
                    raise
                self.last_attempt.pop(identifier, None)
                return {"deleted": identifier, "history_retained": True}

    def publish_notice(self, identifier):
        from agent.admin import sign_notice
        with self.publish_lock:
            with self.lock:
                notice = next((n for n in self.data["notices"] if n["id"] == identifier), None)
                if notice is None:
                    raise ControlError("Notice not found", 404)
                if not self.topic:
                    raise ControlError("No administrator topic", 409)
                if any(t["topic_id"] == notice.get("topic_id") for t in self.data["deleted_topics"]):
                    return deepcopy(notice)
                pending = [i for i, r in notice["receipts"].items()
                           if r["status"] != "applied" and not self._cleanup_job("profile", i)]
                self.last_attempt[identifier] = time.monotonic()
                if not pending or not any(self.runtime.get(i, {}).get("admin_joined") for i in pending):
                    return deepcopy(notice)
                envelope = {"kind": "admin.notice", "notice_id": notice["id"], "content": notice["content"],
                            "targets": pending}
                envelope["signature"] = sign_notice(envelope, self.admin_secret)
                topic_id = self.topic["topic_id"]
                notice["attempts"] += 1
            error = None
            try:
                self.comm.send(topic_id, json.dumps(envelope, ensure_ascii=False))
            except Exception as exc:
                # send() may have partially delivered. Only runtime receipts
                # prove which recipients actually accepted this notice.
                error = f"{type(exc).__name__}: {exc}"
            with self.lock:
                notice["publish_error"] = error
                notice["last_attempt_at"] = timestamp()
                self._save()
                return deepcopy(notice)

    def _maintain(self):
        while not self.closed.wait(1):
            try:
                self._retry_cleanup_jobs()
                with self.lock:
                    should_invite = self.topic and any(
                        r["agent_id"] and not r["admin_joined"] and time.monotonic() - self.last_invite.get(i, 0) > 5
                        and r["state"] not in ("stopped", "stopping", "failed") for i, r in self.runtime.items())
                    pending = [n["id"] for n in self.data["notices"]
                               if any(r["status"] != "applied" for r in n["receipts"].values())
                               and time.monotonic() - self.last_attempt.get(n["id"], 0) > 5]
                if should_invite:
                    self.invite_all()
                self._dispatch_topic_deletions()
                for identifier in pending:
                    if self.closed.is_set():
                        break
                    self.publish_notice(identifier)
                topics = [asdict(t) for t in self.comm.discover_topics()]
                with self.lock:
                    self.topic_cache = topics
                    self.history.remember_topics(topics)
                if self.topic:
                    self.comm.receive(self.topic["topic_id"])
            except Exception:
                # Per-worker and per-notice failures remain visible in state;
                # temporary peer discovery errors must not stop reconciliation.
                continue

    def topic_messages(self, identifier, *, before=None, after=None, limit=50, query=""):
        with self.lock:
            if any(t["topic_id"] == identifier for t in self.data["deleted_topics"]):
                raise ControlError("Topic was deleted", 404)
            known = self.history.topic(identifier) or next(
                (t for t in self.topic_cache if t["topic_id"] == identifier), None)
            if not known and not (self.topic and self.topic["topic_id"] == identifier):
                raise ControlError("Topic not found", 404)
            try:
                return self.history.message_page(identifier, before=before, after=after, limit=limit, query=query)
            except ValueError as exc:
                raise ControlError(str(exc)) from None

    def snapshot(self):
        with self.lock:
            profiles = [{**self._resolved_profile(p), "runtime": deepcopy(self.runtime[p["id"]])} for p in self.data["profiles"]]
            for profile in profiles:
                if job := self._cleanup_job("profile", profile["id"]):
                    profile["runtime"].update(cleanup_pending=True, cleanup_error=job.get("error"))
            topics = {t["topic_id"]: t for t in self.history.topics()}
            for topic in self.topic_cache:
                previous = topics.get(topic["topic_id"], {})
                topics[topic["topic_id"]] = {**previous, **deepcopy(topic), "is_historical": False}
            if self.topic:
                topics[self.topic["topic_id"]] = {**topics.get(self.topic["topic_id"], {}), **deepcopy(self.topic), "is_historical": False}
            for p in profiles:
                for identifier in p["runtime"]["joined_topics"]:
                    topics.setdefault(identifier, {"topic_id": identifier, "name": identifier[:12], "description": ""})
            deleted = {topic["topic_id"] for topic in self.data["deleted_topics"]}
            topics = {identifier: topic for identifier, topic in topics.items() if identifier not in deleted}
            for p in profiles:
                p["runtime"]["joined_topics"] = [t for t in p["runtime"]["joined_topics"] if t not in deleted]
            for topic in topics.values():
                topic["is_admin"] = bool(self.topic and self.topic["topic_id"] == topic["topic_id"])
                topic["can_delete"] = not topic["is_admin"]
                topic["delete_reason"] = "当前管理员通知频道用于全体通知，不能删除" if topic["is_admin"] else None
                if not topic.get("is_historical"):
                    topic["members"] = [{"profile_id": p["id"], "name": p["name"]} for p in profiles
                                        if topic["topic_id"] in p["runtime"]["joined_topics"] or
                                        (topic["is_admin"] and p["runtime"]["admin_joined"])]
                if topic.get("last_message"):
                    topic["last_message"]["content"] = topic["last_message"]["content"][:200]
            return {"csrf_token": self.csrf_token, "credentials_configured": bool(self.provider_keys or self._legacy_key_pending),
                    "model_providers": [self._public_provider(p) for p in self.data["model_providers"]],
                    "admin": {"agent_id": self.comm.agent_id, "endpoint": self.comm.endpoint, "topic": deepcopy(self.topic)},
                    "hosts": deepcopy(self.data["hosts"]), "profiles": profiles, "topics": list(topics.values()),
                    "history_recovery": deepcopy(self.history_recovery),
                    "history_revision": self.history.revision(),
                    "cleanup_jobs": deepcopy(self.data["cleanup_jobs"]),
                    "deleted_profile_ids": list(self.data["deleted_profile_ids"]),
                    "deleted_topic_ids": sorted(deleted),
                    "topic_deletions": [self._topic_deletion_state(t) for t in self.data["deleted_topics"]],
                    "notices": deepcopy(list(reversed(self.data["notices"]))), "events": deepcopy(list(self.events))}

    def close(self):
        if self.closed.is_set():
            return
        self.closed.set()
        for identifier in list(self.processes):
            self.stop_profile(identifier)
        deadline = time.monotonic() + 10
        for record in list(self.processes.values()):
            try:
                record["process"].wait(timeout=max(.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                # Close the control channel; remote worker EOF requests stop.
                try:
                    record["process"].stdin.close()
                except (OSError, ValueError):
                    pass
                record["process"].terminate()
        if self.thread:
            self.thread.join(timeout=3)
        # Drain completed worker output before closing its durable history sink.
        for record in self.processes.values():
            if reader := record.get("reader"):
                reader.join(timeout=3)
        self.comm.close()
        self.history.close()
        self.lease.close()
        self.api_key = ""
        self.provider_keys.clear()
        self.session_secrets.clear()
        self._legacy_key_pending = ""
