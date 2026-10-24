#!/usr/bin/env python3
"""Autonomous topic-driven agent with synchronous streaming and cron wakeups.

The model/tool loop follows LCC s01, scheduling follows s12, and the outer
working/idle lifecycle follows the teammate runtime in s13.
"""

import os
import json
import logging
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

# Directory settings must be loaded before history, memory and cron initialize.
# A launcher's per-process environment takes priority over shared .env values.
load_dotenv(os.getenv("AGENT_ENV_FILE") or Path(__file__).resolve().parent / ".env", override=False)

import cron_scheduler as cron
from agent_runtime import AgentRuntime
from file_tools import TOOLS as FILE_TOOLS, FileTools
from topic_tools import TOOLS as TOPIC_TOOLS, TopicManager
from hooks import register_hook, trigger_hooks
from history import load_history, save_history
import memory

from anthropic import Anthropic

if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL") or None)
MODEL = os.environ["MODEL_ID"]
memory.client = client
memory.MODEL = MODEL
topic_manager: TopicManager | None = None
logger = logging.getLogger("agent")
pending_cron_jobs: list[cron.CronJob] = []
WORKDIR = Path(os.getenv("AGENT_WORKDIR", Path(__file__).resolve().parent)).resolve()
file_tools = FileTools(WORKDIR)

ENVIRONMENT_PROMPT = (
    f"Your workspace is {WORKDIR}. File tools can only access this directory "
    "and its children. Use read_file, write_file, edit_file and glob for files. "
    "Shell execution is unavailable to enforce this boundary."
)
SYSTEM = (
    f"You are a coding agent at {WORKDIR}. "
    "Use tools to solve tasks. Act, don't explain."
)
RUNTIME_PROMPT = (
    "Run autonomously from the system prompt and incoming topic messages. "
    "Use schedule_cron to arrange future work, list_crons to inspect it, and "
    "cancel_cron to cancel it. Cron has five fields and uses Asia/Shanghai (UTC+8). "
    "Scheduled prompts arrive as [Scheduled ...] messages. Finish the current task "
    "and respond when it is done; the harness will wait for messages or scheduled "
    "work and wake you automatically. Do not poll or sleep using tools to wait."
)

# -- Tool definitions --
TOOLS = list(FILE_TOOLS)
TOOLS.extend(TOPIC_TOOLS)
TOOLS.extend(cron.TOOLS)


# -- Hooks --
def collect_runtime_events(messages: list) -> bool:
    incoming = topic_manager.read_messages() if topic_manager is not None else []
    if incoming:
        append_topic_messages(messages, incoming)
    jobs = cron.consume_cron_queue()
    for job in jobs:
        text = f"[Scheduled {job.id} at {job.last_fired}] {job.prompt}"
        # A restored pending delivery can already be present in the saved history.
        if not any(
            message["role"] == "user" and isinstance(message["content"], list)
            and any(isinstance(block, dict) and block.get("text") == text
                    for block in message["content"])
            for message in messages
        ):
            append_user_text(messages, text)
        if all(pending.id != job.id for pending in pending_cron_jobs):
            pending_cron_jobs.append(job)
    return bool(incoming or jobs)


def inject_runtime_events(messages: list):
    collect_runtime_events(messages)


def acknowledge_scheduled_jobs(messages: list, response):
    if pending_cron_jobs:
        # Persist the accepted prompt/response before deleting a one-shot job.
        save_history(messages)
        cron.acknowledge_cron_jobs(pending_cron_jobs)
        pending_cron_jobs.clear()


def append_topic_messages(messages: list, incoming: list, notice: str = ""):
    text = "New topic messages:\n" + json.dumps(incoming, ensure_ascii=False)
    if notice:
        text = notice + "\n\n" + text
    append_user_text(messages, text)


def append_user_text(messages: list, text: str):
    block = {"type": "text", "text": text}
    # Keep tool_result blocks first in the user message.
    if messages and messages[-1]["role"] == "user":
        if isinstance(messages[-1]["content"], str):
            messages[-1]["content"] = [
                {"type": "text", "text": messages[-1]["content"]}
            ]
        messages[-1]["content"].append(block)
    else:
        messages.append({"role": "user", "content": [block]})


def preserve_interrupted_response(messages: list, partial_text: str, incoming: list):
    if partial_text.strip():
        messages.append({
            "role": "assistant",
            "content": [{"type": "text", "text": partial_text}],
        })
        notice = (
            "上一条 assistant 回复因新 topic 消息到达而中断。"
            "以上仅保留截至中断时已输出的文本，不代表任务已经完成。"
        )
    else:
        notice = "上一次模型生成在输出文本前被新 topic 消息中断。"
    notice += "本次被中断响应中的工具调用均未执行。请结合以下新消息继续处理。"
    append_topic_messages(messages, incoming, notice)
    save_history(messages)


def log_tool_call(block):
    logger.info("Tool call: id=%s name=%s input=%s", block.id, block.name, block.input)


def log_tool_result(block, output):
    logger.info("Tool result: id=%s name=%s output=%s", block.id, block.name, output)


register_hook("BeforeModelCall", inject_runtime_events)
register_hook("BeforeModelCall", save_history)
register_hook("ModelInterrupted", preserve_interrupted_response)
register_hook("AfterModelResponse", acknowledge_scheduled_jobs)
register_hook("PreToolUse", log_tool_call)
register_hook("PostToolUse", log_tool_result)
register_hook("AfterToolBatch", save_history)
register_hook("Stop", save_history)
register_hook("AfterTurn", memory.remember_after_turn)


TOOL_HANDLERS = dict(file_tools.handlers)
TOOL_HANDLERS.update(cron.TOOL_HANDLERS)


def execute_tool(block) -> dict:
    output = trigger_hooks("PreToolUse", block)
    if output is None:
        handler = TOOL_HANDLERS.get(block.name)
        if handler is None and topic_manager is not None:
            handler = topic_manager.handlers.get(block.name)
        try:
            output = handler(**block.input) if handler else f"Error: Unknown or disabled tool: {block.name}"
            if not isinstance(output, str):
                output = json.dumps(output, ensure_ascii=False)
        except Exception as exc:
            output = f"Error: {type(exc).__name__}: {exc}"
        trigger_hooks("PostToolUse", block, output)
    else:
        output = str(output)
    return {
        "type": "tool_result",
        "tool_use_id": block.id,
        "content": output,
    }


# -- Stream text immediately; return complete blocks for history and tools --
def stream_response(messages: list, system: str):
    draft = []
    incoming = []
    try:
        with client.messages.stream(
            model=MODEL, system=system, messages=messages,
            tools=TOOLS, max_tokens=8000,
        ) as stream:
            for text in stream.text_stream:
                draft.append(text)
                print(text, end="", flush=True)
                if topic_manager is not None:
                    incoming = topic_manager.read_messages()
                    if incoming:
                        break
            if not incoming:
                return stream.get_final_message()
    finally:
        if draft:
            print()
        if incoming:
            logger.info("Model generation interrupted by topic messages: topics=%s",
                        [item["topic_id"] for item in incoming])
            trigger_hooks("ModelInterrupted", messages, "".join(draft), incoming)
    return None


# -- The core pattern: a while loop that calls tools until the model stops --
def agent_loop(messages: list, system_prompt: str | None = None):
    prompt = SYSTEM if system_prompt is None else system_prompt
    prompt += (f"\n\nAgent name: {os.getenv('AGENT_NAME', 'agent')}\n{ENVIRONMENT_PROMPT}\n{RUNTIME_PROMPT}"
               f"\nCurrent time: {datetime.now(cron.CRON_TIMEZONE).isoformat(timespec='seconds')}")
    system = memory.build_system(prompt, memory.load_memories(messages))
    while True:
        trigger_hooks("BeforeModelCall", messages)
        response = stream_response(messages, system)
        if response is None:
            continue
        messages.append({"role": "assistant", "content": response.content})
        trigger_hooks("AfterModelResponse", messages, response)

        tool_calls = [
            block for block in response.content if block.type == "tool_use"
        ]
        if not tool_calls:
            force = trigger_hooks("Stop", messages)
            if force is not None:
                messages.append({"role": "user", "content": str(force)})
                continue
            trigger_hooks("AfterTurn", messages)
            return

        results = [execute_tool(block) for block in tool_calls]
        messages.append({"role": "user", "content": results})
        trigger_hooks("AfterToolBatch", messages)


def run_agent(initial_prompt: str | None = None, messages: list | None = None, stop_event=None):
    """Run until stopped; all instructions after startup arrive as events."""
    history = load_history() if messages is None else messages
    prompt = SYSTEM if initial_prompt is None else initial_prompt
    cron.load_durable_jobs()
    append_user_text(history, "[Started] 根据系统提示和当前上下文自主开始工作，按需创建定时任务。")
    runtime = AgentRuntime(
        work=lambda: agent_loop(history, prompt),
        collect_work=lambda: collect_runtime_events(history),
        stop_event=stop_event,
    )
    scheduler_thread = cron.start_scheduler(runtime.stop_event)
    try:
        runtime.run()
    except KeyboardInterrupt:
        logger.info("Agent stopped by KeyboardInterrupt")
    except Exception:
        logger.exception("Agent runtime failed")
        raise
    finally:
        runtime.stop_event.set()
        scheduler_thread.join(timeout=2)
        cron.restore_cron_jobs(pending_cron_jobs)
        pending_cron_jobs.clear()
        save_history(history)
    return history


if __name__ == "__main__":
    run_agent()
