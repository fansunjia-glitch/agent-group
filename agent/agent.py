"""A single topic-driven agent using the OpenAI-compatible Chat Completions API."""

from contextlib import ExitStack
from collections.abc import Callable
from datetime import datetime
import json
import logging
import math
from pathlib import Path
from queue import Empty, SimpleQueue
import re
from threading import Event
from time import monotonic
from uuid import uuid4

from openai import OpenAI

from comm import Comm
from .admin import verify_notice
from .cron_scheduler import CRON_TIMEZONE, CronScheduler
from .file_tools import FileTools, TOOLS as FILE_TOOLS
from .history import HistoryStore, build_context
from .identity import resolve_user_id, validate_user_id
from .memory import MemoryStore
from .topic_tools import TopicManager, TOOLS as TOPIC_TOOLS


DEFAULT_PROMPT = (
    "你是一个独立运行的 agent。先发现已有 topic，根据 name 和 description 决定是否加入。"
    "通过 topic 消息与其他 agent 协作；没有合适的 topic 时可以创建。"
    "完成当前任务后结束回复，运行时会等待新消息或定时任务，不要自行轮询或休眠。"
)


class Agent:
    """Own one model client, one Comm node, and one workspace.

    Configuration is explicit; importing this module reads no .env file and
    starts no client, server, scheduler or file handler. Call stop() to request
    shutdown; run() finishes the current synchronous operation and closes all
    owned resources. Use a distinct workdir for each concurrently running node.
    """

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.deepseek.com",
        model: str = "deepseek-flash",
        initial_prompt: str = DEFAULT_PROMPT,
        name: str | None = None,
        workdir: str | Path | None = None,
        host: str | None = None,
        seeds: tuple[str, ...] = (),
        request_timeout: float = 30.0,
        max_tokens: int = 8000,
        max_context_chars: int = 60000,
        thinking: bool = False,
        enable_memory: bool = True,
        idle_interval: float = 0.2,
        persona: str = "",
        profile_id: str | None = None,
        user_id: str | None = None,
        deleted_topic_ids: tuple[str, ...] = (),
        event_sink: Callable[[dict], None] | None = None,
    ) -> None:
        for key, value in (("api_key", api_key), ("base_url", base_url),
                           ("model", model), ("initial_prompt", initial_prompt)):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{key} must be a non-empty string")
        for key, value in (("request_timeout", request_timeout), ("idle_interval", idle_interval)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{key} must be a finite positive number")
        for key, value in (("max_tokens", max_tokens), ("max_context_chars", max_context_chars)):
            if type(value) is not int or value <= 0:
                raise ValueError(f"{key} must be a positive integer")
        if not isinstance(persona, str):
            raise ValueError("persona must be a string")
        if profile_id is not None and (not isinstance(profile_id, str) or not profile_id.strip()):
            raise ValueError("profile_id must be a non-empty string")
        if user_id is not None:
            validate_user_id(user_id)
        if (not isinstance(deleted_topic_ids, (tuple, list))
                or any(not isinstance(topic_id, str) or not topic_id.strip() for topic_id in deleted_topic_ids)):
            raise ValueError("deleted_topic_ids must be a list of non-empty topic IDs")
        if event_sink is not None and not callable(event_sink):
            raise ValueError("event_sink must be callable")
        self.name = name if name is not None else f"agent-{uuid4().hex[:12]}"
        if not isinstance(self.name, str) or not self.name or self.name in (".", "..") or "/" in self.name or "\\" in self.name:
            raise ValueError("name must be a non-empty directory name")
        self.workdir = Path(workdir if workdir is not None else Path("workspaces") / self.name).expanduser().resolve()
        self.model = model
        self.initial_prompt = initial_prompt
        self.persona = persona
        self.profile_id = profile_id or self.name
        self._event_sink = event_sink
        self._admin_invitations = SimpleQueue()
        self._topic_deletions = SimpleQueue()
        self._admin_topic_id = None
        self._admin_sender_id = None
        self._admin_secret = None
        self._retired_admin_topics: set[str] = set()
        self.max_tokens = max_tokens
        self.max_context_chars = max_context_chars
        self.thinking = thinking
        self.idle_interval = idle_interval
        self._stop = Event()
        self._running = False
        self._closed = False
        self._pending_jobs = []
        self.state = "idle"

        # An incomplete constructor also releases everything already acquired.
        with ExitStack() as resources:
            self.workdir.mkdir(parents=True, exist_ok=True)
            self.user_id = resolve_user_id(self.workdir, user_id=user_id, profile_id=profile_id)
            self.history = HistoryStore(self.workdir)
            self.messages = self.history.load()
            # History is also the durable receipt ledger, avoiding a second
            # state file whose commit could disagree after a crash.
            self._applied_notices = {
                match.group(1) for message in self.messages
                if message.get("role") == "user"
                and (match := re.match(r"^\[Administrator notice=([A-Za-z0-9_.:-]{1,128})\]\n",
                                       message.get("content", "")))
            }
            self.logger = logging.getLogger(f"agent.{self.name}.{uuid4().hex}")
            self.logger.setLevel(logging.INFO)
            self.logger.propagate = False
            log_path = self.workdir / ".logs" / "agent.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            handler = logging.FileHandler(log_path, encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            self.logger.addHandler(handler)
            resources.callback(handler.close)
            resources.callback(self.logger.removeHandler, handler)
            self.client = OpenAI(api_key=api_key, base_url=base_url,
                                 timeout=request_timeout, max_retries=0)
            resources.callback(self.client.close)
            self.comm = Comm(host=host, seeds=seeds)
            resources.callback(self.comm.close)
            self.topic_manager = TopicManager(self.comm, event_sink=self._emit_event)
            for topic_id in deleted_topic_ids:
                self.topic_manager.delete_topic(topic_id)
            self.file_tools = FileTools(self.workdir)
            self.scheduler = CronScheduler(self.workdir)
            resources.callback(self.scheduler.close)
            self.memory = MemoryStore(self.workdir, self.client, model, user_id=self.user_id) if enable_memory else None
            self.tools = [*FILE_TOOLS, *TOPIC_TOOLS, *self.scheduler.tools]
            self.handlers = {**self.file_tools.handlers, **self.topic_manager.handlers,
                             **self.scheduler.handlers}
            self._resources = resources.pop_all()
        self._emit("ready", agent_id=self.agent_id, endpoint=self.endpoint, user_id=self.user_id)

    @property
    def agent_id(self) -> str:
        return self.comm.agent_id

    @property
    def endpoint(self) -> str:
        return self.comm.endpoint

    def _emit_event(self, event: dict) -> None:
        if self._event_sink is not None:
            try:
                self._event_sink(event)
            except Exception:
                # An unavailable dashboard must not stop the agent's work.
                pass

    def _emit(self, kind: str, **fields) -> None:
        self._emit_event({"type": kind, **fields})

    def invite_admin_topic(self, topic_id: str, admin_id: str, secret: str) -> None:
        """Queue a trusted supervisor invitation; safe from another thread.

        Joining happens on the runtime thread before polling messages, including
        between streaming chunks. This is deliberately not a model tool.
        """
        for key, value in (("topic_id", topic_id), ("admin_id", admin_id), ("secret", secret)):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{key} must be a non-empty string")
        if self._closed:
            raise RuntimeError("agent is closed")
        self._admin_invitations.put((topic_id, admin_id, secret))

    def _accept_admin_invitations(self) -> None:
        invitation = None
        while True:
            try:
                invitation = self._admin_invitations.get_nowait()
            except Empty:
                break
        if invitation is None:
            return
        topic_id, admin_id, secret = invitation
        try:
            self.topic_manager.protect_admin_topic(topic_id)
        except Exception as exc:
            self._emit("admin_join_failed", topic_id=topic_id, error=str(exc))
            return
        if self._admin_topic_id and self._admin_topic_id != topic_id:
            self._retired_admin_topics.add(self._admin_topic_id)
        self._admin_topic_id, self._admin_sender_id, self._admin_secret = invitation
        self._emit("admin_joined", topic_id=topic_id, admin_id=admin_id)

    def delete_topic(self, topic_id: str) -> None:
        """Queue a trusted supervisor request for application on the runtime thread."""
        if not isinstance(topic_id, str) or not topic_id.strip():
            raise ValueError("topic_id must be a non-empty string")
        if self._closed:
            raise RuntimeError("agent is closed")
        self._topic_deletions.put(topic_id)

    def _apply_topic_deletions(self) -> None:
        while True:
            try:
                topic_id = self._topic_deletions.get_nowait()
            except Empty:
                break
            try:
                self.topic_manager.delete_topic(topic_id)
            except Exception as exc:
                self._emit("topic_delete_failed", topic_id=topic_id, error=str(exc))

    def _read_topics(self) -> list:
        """Read once, validating control messages before any model exposure."""
        self._apply_topic_deletions()
        self._accept_admin_invitations()
        result, pending = [], set()
        for group in self.topic_manager.read_messages():
            topic_id = group["topic_id"]
            if topic_id in self._retired_admin_topics:
                continue
            if topic_id != self._admin_topic_id:
                result.append(group)
                for message in group["messages"]:
                    payload = message["payload"]
                    self._emit("message_received", topic_id=topic_id,
                               sender_id=message["sender_id"], payload=payload,
                               truncated=False)
                continue
            for message in group["messages"]:
                if message["sender_id"] != self._admin_sender_id:
                    continue
                try:
                    notice = json.loads(message["payload"])
                except (TypeError, ValueError):
                    continue
                if not verify_notice(notice, self._admin_secret) or self.profile_id not in notice["targets"]:
                    continue
                notice_id = notice["notice_id"]
                self._emit("notice_received", notice_id=notice_id, topic_id=topic_id)
                if notice_id in self._applied_notices:
                    self._emit("notice_applied", notice_id=notice_id, topic_id=topic_id, duplicate=True)
                elif notice_id not in pending:
                    pending.add(notice_id)
                    result.append({"topic_id": topic_id, "_admin_notice": {
                        "notice_id": notice_id, "content": notice["content"],
                    }})
                    self._emit("message_received", topic_id=topic_id,
                               sender_id=message["sender_id"], notice_id=notice_id,
                               payload=notice["content"], truncated=False)
        return result

    def stop(self) -> None:
        """Request shutdown; safe for a signal handler or another thread."""
        self._stop.set()

    def close(self) -> None:
        """Close owned resources; a running run() performs cleanup on exit."""
        self.stop()
        if self._closed or self._running:
            return
        self._closed = True
        self._resources.close()

    def __enter__(self):
        if self._closed:
            raise RuntimeError("agent is closed")
        return self

    def __exit__(self, *_):
        self.close()

    def run(self) -> None:
        """Work once at startup, then wake on topic messages or scheduled jobs."""
        if self._closed or self._running:
            raise RuntimeError("agent is closed or already running")
        self._running = True
        try:
            self.scheduler.load()
            self.scheduler.start(self._stop)
            self._append_user("[Started] 根据系统提示和当前上下文自主开始工作。")
            first_turn = True
            while not self._stop.is_set():
                if first_turn or self._collect_events():
                    first_turn = False
                    self._set_state("working")
                    self._run_turn()
                else:
                    self._set_state("idle")
                    self._stop.wait(self.idle_interval)
        except KeyboardInterrupt:
            self.stop()
        except Exception:
            self.logger.exception("Agent runtime failed")
            raise
        finally:
            self.stop()
            try:
                self._cancel_pending_tools()
                self.scheduler.restore(self._pending_jobs)
                self._pending_jobs.clear()
                self.history.save(self.messages)
            finally:
                self._set_state("stopped")
                self._running = False
                self.close()

    def _set_state(self, state: str) -> None:
        if self.state != state:
            self.logger.info("State: %s -> %s", self.state, state)
            self.state = state
            self._emit("state", state=state)

    def _append_user(self, text: str) -> None:
        # Retain event boundaries on disk; context building keeps consecutive
        # unanswered events together so none are silently dropped.
        self.messages.append({"role": "user", "content": text})

    def _cancel_pending_tools(self) -> None:
        """Keep history valid if an interrupt arrives during a tool batch."""
        completed = set()
        for message in reversed(self.messages):
            if message["role"] == "tool":
                completed.add(message["tool_call_id"])
            if message["role"] == "assistant":
                for call in message.get("tool_calls", []):
                    if call["id"] not in completed:
                        self.messages.append({
                            "role": "tool", "tool_call_id": call["id"],
                            "content": json.dumps({"error": "Execution interrupted; outcome may be incomplete"}),
                        })
                break

    def _append_topics(self, incoming: list, notice: str = "") -> None:
        ordinary = [group for group in incoming if "_admin_notice" not in group]
        if ordinary:
            self._append_user(notice + "New topic messages:\n" + json.dumps(ordinary, ensure_ascii=False))
        elif notice:
            self._append_user(notice.strip())
        applied = []
        for group in incoming:
            if "_admin_notice" not in group:
                continue
            item = group["_admin_notice"]
            if item["notice_id"] in self._applied_notices:
                continue
            self._append_user(f"[Administrator notice={item['notice_id']}]\n{item['content']}")
            applied.append((item["notice_id"], group["topic_id"]))
        if applied:
            # Commit the context before acknowledging application. The marker
            # survives HistoryStore normalization and is used for recovery.
            self.history.save(self.messages)
            self._applied_notices.update(item[0] for item in applied)
            for notice_id, topic_id in applied:
                self._emit("notice_applied", notice_id=notice_id, topic_id=topic_id)

    def _collect_events(self) -> bool:
        incoming = self._read_topics()
        if incoming:
            self._append_topics(incoming)
        jobs = self.scheduler.consume()
        for job in jobs:
            text = f"[Scheduled {job.id} at {job.last_fired}] {job.prompt}"
            if not any(message.get("role") == "user" and message.get("content") == text
                       for message in self.messages):
                self._append_user(text)
            if all(pending.id != job.id for pending in self._pending_jobs):
                self._pending_jobs.append(job)
        return bool(incoming or jobs)

    def _system_prompt(self) -> str:
        return (
            (f"Persona:\n{self.persona}\n\n" if self.persona.strip() else "")
            + f"{self.initial_prompt}\n\nAgent name: {self.name}; sender_id: {self.agent_id}.\n"
            f"Workspace: {self.workdir}. File tools operate inside this directory.\n"
            "Topic 是你与其他 agent 交流的群聊。根据自己的身份和当下事情，主动管理参与的话题："
            "有新的独立议题、活动、场景或需要持续跟进的事情时，先看已知话题或 discover_topics，"
            "适合的现有话题先 join_topic；没有合适的话题就可以 create_topic，不必等待别人先建群。"
            "创建后自己自动加入，其他成员不会自动加入；可在相关的普通话题里用 send 告知新 topic_id 和加入理由。"
            "需要让别人听到的发言必须通过 send 发送，普通回复不会自动广播。"
            "消息只投递给当时的其他成员，无历史补发、无自我回送；新消息由运行时自动接收。"
            "不要为每句话重复建群，不要对所有群重复发言；事情结束或无关时可 leave_topic，"
            "仍需等待回复或履行承诺时继续留在群中。管理员通知频道不可主动退出或发布，已删除的 topic ID 不可重入。\n"
            "Use schedule_cron for future work; its five fields use Asia/Shanghai (UTC+8). "
            "Finish the current task and return; the runtime waits for new messages and schedules. "
            "Do not poll or sleep using tools.\n"
            f"Current time: {datetime.now(CRON_TIMEZONE).isoformat(timespec='seconds')}"
        )

    def _run_turn(self) -> None:
        self._collect_events()
        if self._stop.is_set():
            return
        system = self._system_prompt()
        if self.memory is not None:
            recalled = self.memory.load_memories(self.messages)
            system = self.memory.build_system(system, recalled)
        while not self._stop.is_set():
            self._collect_events()
            self.history.save(self.messages)
            context = build_context(self.messages, system, self.tools, self.max_context_chars)
            response = self._stream_response(context)
            if response is None:
                continue
            calls = response.get("tool_calls", [])
            self.messages.append(response)
            for call in calls:
                # Complete the protocol even on stop, without executing any
                # further tools. The saved history remains usable on restart.
                if self._stop.is_set():
                    result = {"role": "tool", "tool_call_id": call["id"],
                              "content": json.dumps({"error": "Agent stopped; tool was not executed"})}
                else:
                    result = self._execute_tool(call)
                self.messages.append(result)
            self.history.save(self.messages)
            if self._pending_jobs:
                self.scheduler.acknowledge(self._pending_jobs)
                self._pending_jobs.clear()
            if self._stop.is_set():
                return
            if not calls:
                if self.memory is not None:
                    stored = self.memory.extract_memories(self.messages)
                    if stored and not self._stop.is_set():
                        self.memory.consolidate_memories()
                return

    def _execute_tool(self, call: dict) -> dict:
        self._apply_topic_deletions()
        name = call["function"]["name"]
        started = monotonic()
        status = "completed"
        self._emit("tool_started", name=name, call_id=call["id"])
        try:
            arguments = json.loads(call["function"]["arguments"])
            if not isinstance(arguments, dict):
                raise ValueError("tool arguments must be a JSON object")
            handler = self.handlers.get(name)
            if handler is None:
                raise ValueError(f"Unknown tool: {name}")
            self.logger.info("Tool: %s id=%s", name, call["id"])
            output = handler(**arguments)
            if not isinstance(output, str):
                output = json.dumps(output, ensure_ascii=False)
        except Exception as exc:
            status = "error"
            self.logger.warning("Tool failed: %s: %s", name, exc)
            output = json.dumps({"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False)
        self._emit("tool_finished", name=name, call_id=call["id"], status=status,
                   duration_ms=round((monotonic() - started) * 1000, 2),
                   result=output[:2000], truncated=len(output) > 2000)
        return {"role": "tool", "tool_call_id": call["id"], "content": output}

    def _preserve_interrupted(self, text: str, incoming: list, reasoning: str = "") -> None:
        if text.strip():
            partial = {"role": "assistant", "content": text, "_interrupted": True}
            if reasoning or self.thinking:
                partial["reasoning_content"] = reasoning
            self.messages.append(partial)
        notice = "上一轮生成被新 topic 消息中断；已输出文本不代表任务完成，未完成的工具调用均未执行。\n\n"
        self._append_topics(incoming, notice)
        self.history.save(self.messages)

    def _stream_response(self, messages: list[dict]) -> dict | None:
        if self._stop.is_set():
            return None
        started = monotonic()
        status, output, error = "error", "", None
        self._emit("model_started", model=self.model)
        try:
            response = self._generate_response(messages)
            status = "completed" if response is not None else "stopped" if self._stop.is_set() else "interrupted"
            if response is not None:
                output = response.get("content") or ""
            return response
        except Exception as exc:
            error = str(exc)[:1000]
            raise
        finally:
            self._emit("model_finished", model=self.model, status=status,
                       duration_ms=round((monotonic() - started) * 1000, 2),
                       output=output[:2000], truncated=len(output) > 2000, error=error)

    def _generate_response(self, messages: list[dict]) -> dict | None:
        if self._stop.is_set():
            return None
        text, reasoning, calls = [], [], {}
        finish_reason = None
        try:
            with self.client.chat.completions.create(
                model=self.model, messages=messages, tools=self.tools,
                stream=True, max_tokens=self.max_tokens,
                extra_body={"thinking": {"type": "enabled" if self.thinking else "disabled"},
                            "user_id": self.user_id},
            ) as stream:
                for chunk in stream:
                    if self._stop.is_set():
                        return None
                    if not chunk.choices:
                        incoming = self._read_topics()
                        if incoming:
                            self._preserve_interrupted("".join(text), incoming, "".join(reasoning))
                            return None
                        continue
                    choice = chunk.choices[0]
                    delta = choice.delta
                    if delta.content:
                        text.append(delta.content)
                        print(delta.content, end="", flush=True)
                    if value := getattr(delta, "reasoning_content", None):
                        reasoning.append(value)
                    for part in delta.tool_calls or []:
                        call = calls.setdefault(part.index, {"id": "", "type": "function",
                                                            "function": {"name": "", "arguments": ""}})
                        if part.id:
                            call["id"] = part.id
                        if part.function is not None:
                            call["function"]["name"] += part.function.name or ""
                            call["function"]["arguments"] += part.function.arguments or ""
                    finish_reason = choice.finish_reason or finish_reason
                    incoming = self._read_topics()
                    if incoming:
                        self._preserve_interrupted("".join(text), incoming, "".join(reasoning))
                        return None
        except Exception:
            if self._stop.is_set():
                return None
            raise
        finally:
            if text:
                print()
        if self._stop.is_set():
            return None
        if finish_reason not in ("stop", "tool_calls"):
            raise RuntimeError(f"Incomplete model response ({finish_reason}); no tools were executed")
        response = {"role": "assistant", "content": "".join(text) or None}
        if calls:
            ordered = [calls[index] for index in sorted(calls)]
            if any(not call["id"] or not call["function"]["name"] for call in ordered):
                raise RuntimeError("Model returned an incomplete tool call")
            response["tool_calls"] = ordered
        elif not text:
            raise RuntimeError("Model returned neither content nor tool calls")
        # DeepSeek requires reasoning_content to be replayed when thinking
        # mode and tools are used together. Never print it as user output.
        if reasoning or self.thinking:
            response["reasoning_content"] = "".join(reasoning)
        return response
