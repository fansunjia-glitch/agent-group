# Agent Harness

常驻运行的多 agent harness，模型与工具循环参考 Learn Claude Code。每个 agent 在独立进程中运行，根据初始提示词自主工作，通过 topic 消息和定时任务接收后续工作，不读取终端用户输入。

## 安装与配置

运行真实通信后端要求 **Python 3.14+、macOS/Linux**，与仓库的 `comm` 组件一致。Windows 原生运行暂不支持；Windows 可以检查配置，实际运行须使用符合条件的 Linux / macOS 环境。

从仓库根目录安装并准备配置：

```sh
cd agent
python3.14 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
# 已有 .env 时请保留原文件和密钥。
[ -f .env ] || cp .env.example .env
```

编辑 `.env`。所有配置均由这个文件加载，`.env.example` 提供完整的三个 agent 示例。

### 公共模型设置

```dotenv
ANTHROPIC_API_KEY=填写你的模型服务密钥
ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic
MODEL_ID=填写该服务实际可用的模型名称
```

模型请求使用 Anthropic SDK。使用 DeepSeek 时填写其 Anthropic 兼容接口地址和 DeepSeek API key，变量名仍保留 `ANTHROPIC_` 前缀。同一组 agent 共用这些模型设置，进程之间各自维护上下文。不要将真实 `.env` 提交到 Git。

### 单个 agent

```dotenv
AGENTS=worker
TOPICS=tasks,results
COMM_HOST=127.0.0.1

AGENT_WORKER_WORKDIR=./workspaces/worker
AGENT_WORKER_TOPICS=tasks,results
AGENT_WORKER_INITIAL_PROMPT="你是任务执行 agent。
启动后等待 tasks 中的消息，完成任务后将结果作为字符串发送到 results。
需要文件操作时只使用自己的工作目录，当前任务完成后等待后续消息。"
```

### 多个 agent

将 `AGENTS` 改为逗号分隔的名称，并为每个名称提供配置：

```dotenv
AGENTS=worker,reviewer
TOPICS=tasks,drafts,results
COMM_HOST=127.0.0.1

AGENT_WORKER_WORKDIR=./workspaces/worker
AGENT_WORKER_TOPICS=tasks,drafts,results
AGENT_WORKER_INITIAL_PROMPT="处理 tasks 中的任务，将待复核结果发送到 drafts。"

AGENT_REVIEWER_WORKDIR=./workspaces/reviewer
AGENT_REVIEWER_TOPICS=drafts,results
AGENT_REVIEWER_INITIAL_PROMPT="复核 drafts 中的结果，将结论发送到 results。"
```

配置规则：

| 配置 | 说明 |
| --- | --- |
| `AGENTS` | 要启动的 agent 名称，至少一个；每个名称对应一个独立进程 |
| `AGENT_<名称>_WORKDIR` | 必填，工具和运行数据使用的工作目录，各 agent 必须不同 |
| `AGENT_<名称>_INITIAL_PROMPT` | 必填，初始系统提示词；双引号内支持多行 |
| `AGENT_<名称>_TOPICS` | 可选，启动时订阅的 topic 名称；留空表示暂不订阅 |
| `TOPICS` | 启动时预建或加入的 topic 名称，须包含配置中引用的订阅名称 |
| `COMM_HOST` | 可选，本机网卡 IPv4；留空自动选择并启用局域网发现，`127.0.0.1` 仅同机通信 |
| `COMM_SEEDS` | 可选，逗号分隔的已有节点 HTTP 地址；正常同机／局域网发现无需填写 |
| `COMM_TIMEOUT` | 可选，通信请求超时秒数，默认 `5`，必须为有限正数 |
| `AGENT_STARTUP_TIMEOUT` | 可选，等待同组全部 agent 订阅完成的超时秒数，默认 `60` |

名称以英文字母开头，后续可使用字母、数字、下划线和连字符。环境变量前缀使用大写，连字符转为下划线，例如 `review-agent` 对应 `AGENT_REVIEW_AGENT_`。转换后的前缀不得重复。

配置中的相对路径以 `.env` 所在目录为基准；Windows 路径建议使用 `/`。启动器会创建工作目录，设置各进程的 `AGENT_NAME`、`AGENT_WORKDIR` 和当前目录，再加载 agent 模块。不需要配置公共 `AGENT_WORKDIR`。

## 启动

先检查配置，再启动：

```sh
python launch_agents.py --check
python launch_agents.py
```

`--check` 不调用模型、不启动 agent，不显示密钥或完整提示词。实际启动前必须填入有效的 API key。

只启动名单中的一个 agent：

```sh
python launch_agents.py --agent worker
```

使用另一份配置文件：

```sh
python launch_agents.py --env /path/to/team.env --check
python launch_agents.py --env /path/to/team.env
```

不带 `--agent` 时启动 `AGENTS` 中的全部 agent，每个 agent 一个子进程、一个独立 `Comm` 实例，另有一个启动器进程。只配置 `AGENTS=worker` 时也使用相同启动流程。Ctrl+C 请求这一组进程停止。启动器发现子进程退出时也会停止同组其他进程，故障退出码会返回到终端。

也可在仓库根目录运行 `python agent/launch_agents.py` 或 `python -m agent.launch_agents`，默认仍读取 `agent/.env`。

启动后 agent 根据初始提示词开始首次工作；调用模型、执行工具并继续处理工具结果，直到当前任务完成。没有新消息和到期任务时进入等待，每 0.2 秒检查一次待处理事件，不调用模型。收到 topic 消息或到期任务后恢复工作。

后续指令须通过通信接口的 `send(topic_id, payload)` 发送，`payload` 为字符串。agent 不从终端读取指令；配置中的初始订阅从加入时的末尾开始，启动前发送的历史消息不会自动补发。

## 通信与工具

启动器通过 `communication.py` 直接创建仓库的 `comm.Comm`，没有 SQLite 或模拟通信后备。请将本目录与 `comm/` 保持为仓库中的同级目录。

启动时，启动器临时创建一个 `Comm` 节点，发现并加入已有的配置 topic，或在不存在时创建。它把 topic ID 和临时 seed 地址写入本次运行的 `topics.json`，各子进程据此加入同一组 topic。所有子进程确认订阅完成后，启动器关闭临时节点并放行模型调用；后续消息在 agent 之间直接传递，不经过启动器。如果订阅未在配置时间内完成，该组进程停止，错误写入日志。

名称用于启动配置，实际通信使用唯一 topic ID。发现到同名的多个 topic 时，启动器报错，避免随意选择不同频道。跨机器加入已有团队时先启动一台，再启动其他机器，让它们发现已有 topic；同时各自创建同名 topic 不会自动合并。没有任何成员订阅的 topic 会消失；只预建但无人订阅的 topic 在临时节点关闭后也会消失。

每个 `Comm` 自动生成唯一 `sender_id`，与 `.env` 中的 agent 名称分别管理，避免不同机器使用相同配置时 ID 冲突。名称、`sender_id` 和 HTTP endpoint 的对应关系写入各 agent 日志和本次运行的 `ready/<名称>.json`。收件保留 `sender_id` 及原始字符串 `payload`，不解析或改写 payload。

同机默认优先通过共享内存投递，跨机器由 `comm` 使用点对点 HTTP，自动发现使用局域网 UDP 组播。需要指定网卡时设置 `COMM_HOST` 为该机器的实际 IPv4；它不是其他 agent 的地址。受限网络可配置一个已有节点的 `COMM_SEEDS`。具体传输和资源容量说明见 [comm 文档](../comm/README.md)。通信组件不提供消息持久化或异常进程退出后的共享内存资源回收。

`TopicManager` 对接六方法通信接口：`create_topic`、`discover_topics`、`join_topic`、`send`、`receive`、`leave_topic`。`TopicInfo` 与 `Message` 数据类转换为模型上下文可使用的 JSON 对象。其中 `receive` 由 harness 在模型调用前、文本流每个片段输出后和等待状态下读取；其余五个方法作为模型工具提供。发送只给其他已加入成员，不会回送给自己。

文件工具包括 `read_file(path, limit=None)`、`write_file(path, content)`、`edit_file(path, old_text, new_text)` 和 `glob(pattern)`。`edit_file` 只替换第一次精确匹配，`glob` 支持 `**/*.py` 等模式。

文件工具统一限制在该 agent 的工作目录内，检查解析后的真实路径，并拒绝越界访问、外部链接、硬链接文件及 Windows 设备路径和数据流。glob 不遍历符号链接和 junction。当前不提供 Bash 或任意代码执行工具，目录限制属于工具层约束。

定时工具为 `schedule_cron`、`list_crons` 和 `cancel_cron`。cron 使用北京时间（UTC+8）的五段表达式，例如 `0 9 * * *` 表示每天 09:00。到期 prompt 进入消息队列唤醒 agent；定时线程本身不调用模型。

## 运行数据与恢复

每个工作目录分别保存：

| 路径 | 内容 |
| --- | --- |
| `.logs/agent.log` | 状态切换、工具调用和运行错误 |
| `.logs/console.log` | 启动器收集的模型流式输出及标准错误 |
| `.logs/history.json` | 当前对话上下文，启动时自动恢复 |
| `.memory/` | 长期记忆和索引 |
| `.scheduled_tasks.json` | 持久化的定时任务及待交付状态 |

启动器日志保存于 `.env` 所在目录的 `.logs/launcher/<运行编号>/launcher.log`。

流式生成期间到达的新 topic 消息会关闭当前流，保留已经输出的部分文本，并在下一条消息中注明中断后重新调用模型。被中断响应中的工具调用不执行。工具执行期间不会中断工具，消息在后续模型调用前注入。

历史和持久化定时任务默认恢复；长期记忆在工作阶段召回，并在任务完成后按需提取。记忆处理可能额外调用模型。不要将工作目录、日志、历史、记忆或通信数据库提交到 Git。
