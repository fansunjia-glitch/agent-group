"""Start one independent agent. Edit the explicit settings below; no .env."""

from pathlib import Path
import signal
from uuid import uuid4

from agent import Agent


API_KEY = ""  # 填写所选供应商的 API key；不会从环境变量或 .env 读取。
BASE_URL = "https://api.deepseek.com"  # 可替换为任意 OpenAI 兼容服务的 API 根地址。
MODEL = "deepseek-flash"  # 改成该服务提供的模型 ID。
API_MODE = None  # 默认按地址推断；通用服务为 openai，DeepSeek 代理可显式设为 deepseek。
NAME = None  # 默认每次创建独立节点名称。
WORKDIR = None  # 设置已有目录可恢复历史；并行节点必须使用不同目录。
INITIAL_PROMPT = (
    "你是独立运行的协作 agent。启动后发现已有 topic，"
    "根据名称和描述判断是否加入，通过消息与其他 agent 协作。"
    "需要时创建 topic。完成当前任务后结束回复，等待后续消息或定时任务。"
)


def main() -> None:
    if not API_KEY.strip():
        raise SystemExit("请在 launch_agent.py 中填写 API_KEY，或在 Python 中直接构造 Agent(api_key=...)。")
    name = NAME or f"agent-{uuid4().hex[:12]}"
    workdir = WORKDIR or Path(__file__).resolve().parent / "workspaces" / name
    with Agent(api_key=API_KEY, base_url=BASE_URL, model=MODEL, api_mode=API_MODE,
               name=name, workdir=workdir, initial_prompt=INITIAL_PROMPT) as agent:
        def request_stop(signum, frame):
            agent.stop()

        previous = {sig: signal.signal(sig, request_stop) for sig in (signal.SIGINT, signal.SIGTERM)}
        try:
            print(f"name={agent.name} agent_id={agent.agent_id} endpoint={agent.endpoint}")
            print(f"workdir={agent.workdir}")
            agent.run()
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)


if __name__ == "__main__":
    main()
