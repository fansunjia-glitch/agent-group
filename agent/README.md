# 独立 Agent 节点

每次启动运行一个进程、一个 `Agent` 和一个 `Comm`。节点通过已有的去中心化 topic 通信协作，同机使用共享内存，远程使用 HTTP；无需配置 agent 名单或启动屏障。

运行环境：Python 3.14+、macOS/Linux。依赖统一在仓库根目录 `pyproject.toml`，`uv.lock` 固定本次验证的版本。

## 安装和启动

在仓库根目录运行：

```sh
uv sync
```

编辑根目录 `launch_agent.py` 的 `API_KEY`，按需调整 `BASE_URL`、`MODEL` 和 `INITIAL_PROMPT`，然后直接启动：

```sh
uv run python launch_agent.py
```

配置通过 Python 参数传入。启动器和 agent 都不加载 `.env`，也不从环境变量读取模型或节点配置。模型客户端和 Comm 由 `Agent` 内部创建和关闭。

默认模型参数：

| 参数 | 默认值 |
| --- | --- |
| `base_url` | `https://api.deepseek.com` |
| `model` | `deepseek-flash` |
| `thinking` | `False`，显式关闭思考模式 |
| `request_timeout` | `30.0` 秒；SDK 自动重试关闭 |
| `max_tokens` | `8000` |

使用 OpenAI SDK 的 `chat.completions.create()` 协议，包括流式文本、函数工具调用和工具结果回传。模型名称与地址依据 [DeepSeek 官方接口说明](https://api-docs.deepseek.com/)；工具和思考字段遵循 [工具调用](https://api-docs.deepseek.com/guides/tool_calls/) 与 [思考模式](https://api-docs.deepseek.com/guides/thinking_mode/) 文档。`thinking=True` 时会保存并回传 `reasoning_content`，不把它打印为回答。

每次启动默认生成新的名称和独立 `workspaces/<name>/` 工作目录。多开进程即可运行多个节点。显式设置 `WORKDIR` 为已有目录时恢复其历史、记忆和定时任务；并行运行的节点必须使用不同目录。

## 在代码中使用

```python
from agent import Agent

with Agent(
    api_key="填写 DeepSeek API key",
    base_url="https://api.deepseek.com",
    model="deepseek-flash",
    initial_prompt="发现任务讨论 topic，根据名称和描述决定加入，协作完成任务。",
    name="worker",
    workdir="./workspaces/worker",
) as agent:
    agent.run()
```

`run()` 在当前进程中运行，完成启动任务后进入等待；topic 消息或到期任务会唤醒它。`stop()` 请求退出，`close()` 幂等清理资源，`run()` 的退出路径也会自动清理。可读取 `agent_id`、`endpoint`、`name`、`workdir` 和 `state`。

其他可选参数：`host=None`、`seeds=()` 传给 Comm；`enable_memory=True` 控制长期记忆；`idle_interval=0.2` 控制等待期间的检查间隔；`max_context_chars=60000` 控制模型请求的近似输入预算。

启动器处理 SIGINT/Ctrl+C 和 SIGTERM，仅设置停止标记。模型流的每个分片、每次模型请求以及工具之间都会检查停止状态。正在阻塞的同步请求仍需返回或超时，正在运行的同步工具允许完成；停止后不开始下一次工具或模型调用，也不通过强杀子进程退出。

## 处理流程

1. 恢复本节点历史与定时任务，加入启动提示。
2. 收集已加入 topic 的消息及到期任务，按需要召回记忆。
3. 构造受预算约束的请求，接收流式回答及分片工具参数。
4. 完整生成后执行工具，以 OpenAI `role=tool` 消息回传结果，再继续调用模型。
5. 没有工具调用时完成当前工作，提取长期记忆并进入等待。

模型通过 `discover_topics` 获取 `topic_id`、`name`、`description`，自行决定加入或创建 topic。启动器不预建 topic、不以名称代替 topic ID。接收器保持 `sender_id` 与原始字符串 `payload`，不解析 payload 内部字段。

生成期间如果收到 topic 消息，会关闭当前流，保存已有文本并标记为未完成回复；未完成的工具调用不会执行。思考和工具参数分片期间也会检查新消息。工具执行期间的新消息在下一次模型请求前收集。

## Web 控制台

在仓库根目录运行 `uv run python launch_control.py`，打开 `http://127.0.0.1:8765`。
控制台支持人设配置、本机与局域网 SSH 启停、Topic 拓扑、运行事件，以及带回执和离线补收的管理员通知。
模型 API key 在页面中设置，仅保存在本次服务内存。配置和独立工作目录保存在 `.control/`。
详细要求与操作步骤见 [控制台说明](../control/README.md)。

## 文件职责

| 文件 | 职责 |
| --- | --- |
| `../launch_agent.py` | 显式启动参数、单节点入口、退出信号 |
| `agent.py` | `Agent` 生命周期、OpenAI 工具循环与资源清理 |
| `topic_tools.py` | 把 Comm 接口暴露为模型工具，读取订阅消息 |
| `file_tools.py` | 工作目录内的读、写、编辑及 glob |
| `history.py` | `HistoryStore`、旧格式转换、请求上下文预算 |
| `memory.py` | 每个节点独立的 `MemoryStore` |
| `cron_scheduler.py` | 每个节点独立的 `CronScheduler` |
| `test/` | 不调用真实模型的回归测试 |

模块导入不会创建模型客户端、Comm 服务或工作目录。状态都归具体实例所有，工作目录在构造时固定，不依赖修改进程当前目录。

## 历史、记忆和定时任务

历史保存在 `.logs/history.json`，日志保存在 `.logs/agent.log`，长期记忆保存在 `.memory/`，定时任务保存在 `.scheduled_tasks.json`。这些目录和默认工作目录已加入 Git 忽略规则。

历史完整保存到磁盘，模型请求只取预算内的最近完整轮次。预算包括 system、召回记忆、工具定义和对话消息；它按序列化字符数计算，并非精确 token 计数，须按所选模型留出输出空间。工具调用及对应结果不会被拆开；尚未处理的连续消息、被中断的当前轮也不能互相裁剪。最新一轮本身超出预算时明确报错，保留历史供调整预算后恢复。

原先 Anthropic 文本、`tool_use`、`tool_result` 格式的历史会转换为 OpenAI 格式；不支持的非文本内容或缺失工具结果会明确报错，避免静默丢数据。正常停止或 KeyboardInterrupt 会为未完成工具补充明确的取消/中断结果。

长期记忆支持召回、提取和整理，同名或同描述的更正会更新现有记录。辅助记忆请求统一关闭 thinking；`enable_memory=False` 可禁用这些辅助请求而保留对话历史。

定时任务支持五字段 cron，时区固定 UTC+8，支持 `*`、数字、范围、列表和 `*/n`，不支持 `a-b/n`。日/月步进从 1 开始，例如 `0 9 */2 * *` 对应 1、3、5……日的 09:00。调度线程只排队，不调用模型。一次性任务在提示送入模型并获得完整响应后确认删除。

## 测试

在根目录运行全部 agent 和 comm 测试：

```sh
uv run python -m unittest discover -v
```

真实组播测试默认跳过，需要本机有局域网 IPv4 路由并允许组播：

```sh
COMM_TEST_LAN=1 uv run python -m unittest discover -v
```

Agent 测试使用模拟模型或本机 HTTP/SSE 服务，不需要真实 API key，也不访问 DeepSeek。保留了两个真实 Comm 节点的消息测试。没有做 200/1024 节点规模验证。

本次本地验证使用系统 CPython 3.14.0。若组播测试在某个解释器中没有响应，可以先用系统 Python 单独运行同一测试进行对照；本机曾出现系统 Python 可用、uv 管理的 Python 3.14.3 不可用的差异。可用 `uv sync --python /path/to/python3.14` 指定已经验证可用的解释器，组播测试仍以真实收发结果为准。
