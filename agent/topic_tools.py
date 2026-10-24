"""Wrap the topic communication interface as model tools."""

from dataclasses import asdict, is_dataclass

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "create_topic",
            "description": (
                "主动建立一个新的聊天话题，创建成功后你已自动加入，返回真实 topic_id。"
                "当出现与当前群聊不同的新议题、需要持续商量的活动或计划、某个地点的日常交流、"
                "一件需要单独跟进的事情，或当前讨论分出独立支线时，都可以创建；不必等管理员或别人要求。"
                "例如面包店闲聊转为周末集市筹备、发现花圃问题后另开排查讨论。"
                "先根据已知话题或调用 discover_topics 检查是否有合适的现有话题：同一件事优先复用，"
                "不要为每一句话、一次简短回复或仅仅改个名称重复建群。"
                "创建不会自动邀请或加入其他 agent，也不会向他们补发历史。"
                "可在你已加入且相关成员所在的普通 topic 中发送新话题的名称、topic_id 和加入理由，"
                "请他们自行 join_topic；待对方加入后再交流细节。新建 topic 是可被发现的普通话题，不是私密群。"
                "不得通过重建同一议题来绕过管理员明确结束讨论的要求。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "简短、具体的聊天主题或场景，例如‘溪桥镇周末集市筹备’、‘花圃翻土情况核对’；避免泛称‘聊天’或给同一件事重复命名。"
                    },
                    "description": {
                        "type": "string",
                        "default": "",
                        "description": "说明讨论什么、为什么开这个话题、哪些人可能想参与及当前需要商量的事，帮助其他 agent 判断是否加入；不要写敏感信息或假定别人已加入。"
                    }
                },
                "required": [
                    "name"
                ]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "discover_topics",
            "description": (
                "查看当前可发现的 topic，返回 topic_id、name、description；没有则返回 []。"
                "开始参与交流、遇到新议题、收到别人提到的新群聊，或准备建群时，可用它寻找合适的对话场所。"
                "根据自己的身份、兴趣、收到的邀请和待办，主动选择相关话题，不必只留在启动时的第一个群。"
                "发现不等于加入，也不会取得聊天历史：要参与现有话题，需使用返回的真实 topic_id 调用 join_topic。"
                "没有合适的话题时可以 create_topic。此结果不是你的已加入清单，也不是全网永久目录；"
                "不要循环调用以等待新话题或新消息，完成当前事情后结束回复，由运行时等待后续事件。"
            ),
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "join_topic",
            "description": (
                "主动加入一个已有 topic，成为成员后才能在里面发送并接收后续消息。"
                "发现与你有关的议题、被普通话题中的消息邀请、需要提供信息或参与协作，"
                "以及讨论转移到新群聊时，都应考虑及时加入，不必等待管理员邀请。"
                "使用 discover_topics 或可信工具结果中取得的真实 topic_id，不用名称代替 ID，也不编造 ID。"
                "加入成功不会重放加入之前的消息；如缺少背景，可以加入后发言请求简要说明。"
                "运行时会自动将已加入话题的新消息交给你，无需手动读取或轮询；重复加入同一 topic 无需进行。"
                "已被管理员删除的 topic ID 不能重新加入，管理员通知频道的成员关系由平台维护。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "topic_id": {
                        "type": "string",
                        "description": "要加入的现有话题的真实 topic_id；先依据其名称和说明确认与你有关，而不是把所有发现的话题都加入。"
                    }
                },
                "required": [
                    "topic_id"
                ]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "send",
            "description": (
                "在你已加入的普通 topic 中发送聊天内容，只投递给发送时已经加入的其他成员，不给自己回送。"
                "模型的普通最终回复不会自动变成群聊消息；要让居民听到你的话，请调用本工具。"
                "按主题选择正确的话题，不要把所有新事情都塞进最早加入的群或管理员通知频道。"
                "若讨论变成新的独立议题，可先 discover_topics，再加入适合的已有群或 create_topic；"
                "在原群简短说明新话题名称和真实 ID，邀请相关成员自行加入，随后到新群继续。"
                "别人尚未加入时发出的消息不会在其加入后补发，发送成功也不等于有人收到或已经回复。"
                "发言保持符合自己身份、与话题相关且有新增信息，不替其他人编造回复，不在多个群重复刷屏。"
                "管理员通知频道仅管理员可发布；已被管理员删除的 topic 不可发送。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "topic_id": {
                        "type": "string",
                        "description": "本次内容应发往的普通话题 ID；必须先 join_topic，或使用 create_topic 返回且已自动加入的 ID。"
                    },
                    "payload": {
                        "type": "string",
                        "description": "你实际要说的话，原样发送。可以是问题、回应、进展、需要协商的新想法，或带真实 topic_id 的转场邀请；不要只描述‘我已发消息’而不写正文。"
                    }
                },
                "required": [
                    "topic_id",
                    "payload"
                ]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "leave_topic",
            "description": (
                "主动退出一个普通 topic，结束自己对它的订阅，停止后续收发，并丢弃自己尚未读取的消息。"
                "当事情已结束、议题与你无关、不再需要跟进、被明确要求离开，或讨论已转移且原群无需继续关注时，"
                "可选择退出，避免长期留在所有群里。必要时先用 send 简短交代结果、去向或交接，再退出。"
                "不要仅因刚说完一句话、暂时安静或还在等待别人答复就退出；仍有承诺、持续协作或重要后续消息时保留订阅。"
                "退出只影响你，不会删除其他成员的话题，也不会自动让其他人退出。"
                "以后如话题仍存在且未被管理员删除，可以重新加入，但不会补收离开期间的消息。"
                "管理员通知频道由平台管理，不能通过本工具退出。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "topic_id": {
                        "type": "string",
                        "description": "你决定不再参与的普通话题 ID；确认已完成必要回应或交接，不要传管理员通知频道 ID。"
                    }
                },
                "required": [
                    "topic_id"
                ]
            }
        }
    }
]


class TopicManager:
    def __init__(self, communication, event_sink=None):
        self.communication = communication
        self.event_sink = event_sink
        self.joined_topics: set[str] = set()
        self.admin_topic_id: str | None = None
        self._protected_topics: set[str] = set()
        self.deleted_topics: set[str] = set()
        self._names: dict[str, str] = {}
        self.handlers = {
            "create_topic": self.create_topic,
            "discover_topics": self.discover_topics,
            "join_topic": self.join_topic,
            "send": self.send,
            "leave_topic": self.leave_topic,
        }

    def _emit(self, kind: str, **fields) -> None:
        if self.event_sink is not None:
            try:
                self.event_sink({"type": kind, **fields})
            except Exception:
                # Observability cannot make an already-completed operation fail.
                pass

    def create_topic(self, name: str, description: str = "") -> str:
        topic_id = self.communication.create_topic(name, description)
        self.joined_topics.add(topic_id)
        self._names[topic_id] = name
        self._emit("topic_joined", topic_id=topic_id, name=name, description=description)
        return topic_id

    def discover_topics(self) -> list:
        """Turn Comm's TopicInfo dataclasses into JSON-compatible tool output."""
        topics = [asdict(topic) if is_dataclass(topic) else topic
                  for topic in self.communication.discover_topics()]
        topics = [topic for topic in topics if topic["topic_id"] not in self.deleted_topics]
        self._names.update({topic["topic_id"]: topic["name"] for topic in topics})
        return topics

    def join_topic(self, topic_id: str) -> None:
        self._require_not_deleted(topic_id)
        if topic_id in self._protected_topics and topic_id != self.admin_topic_id:
            raise PermissionError("Administrator topic membership is managed by the platform")
        joined = topic_id in self.joined_topics
        self.communication.join_topic(topic_id)
        self.joined_topics.add(topic_id)
        if not joined:
            self._emit("topic_joined", topic_id=topic_id, name=self._names.get(topic_id, ""))

    def protect_admin_topic(self, topic_id: str) -> None:
        """Runtime-only invitation acceptance; not exposed as a model tool."""
        self._require_not_deleted(topic_id)
        previous = self.admin_topic_id
        self.communication.join_topic(topic_id)
        self.joined_topics.add(topic_id)
        self._protected_topics.add(topic_id)
        self.admin_topic_id = topic_id
        if previous != topic_id:
            self._emit("topic_joined", topic_id=topic_id, administrator=True)
        if previous is not None and previous != topic_id:
            self.communication.leave_topic(previous)
            self.joined_topics.discard(previous)
            self._emit("topic_left", topic_id=previous, administrator=True)

    def send(self, topic_id: str, payload: str) -> None:
        self._require_not_deleted(topic_id)
        if topic_id in self._protected_topics:
            raise PermissionError("Only the administrator may publish to the administrator topic")
        self.communication.send(topic_id, payload)
        self._emit("message_sent", topic_id=topic_id,
                   sender_id=self.communication.agent_id, payload=payload[:2000],
                   truncated=len(payload) > 2000)

    def leave_topic(self, topic_id: str) -> None:
        if topic_id in self._protected_topics:
            raise PermissionError("Administrator topic membership is managed by the platform")
        joined = topic_id in self.joined_topics
        self.communication.leave_topic(topic_id)
        self.joined_topics.discard(topic_id)
        if joined:
            self._emit("topic_left", topic_id=topic_id)

    def _require_not_deleted(self, topic_id: str) -> None:
        if topic_id in self.deleted_topics:
            raise PermissionError("This topic was deleted by the administrator")

    def delete_topic(self, topic_id: str) -> None:
        """Apply a trusted supervisor tombstone; deliberately not a model tool.

        Other independent Comm nodes may retain the topic. The tombstone keeps
        this managed agent from rediscovering or rejoining that same ID.
        """
        if topic_id == self.admin_topic_id:
            raise PermissionError("The active administrator topic cannot be deleted")
        self.communication.leave_topic(topic_id)
        self.deleted_topics.add(topic_id)
        self.joined_topics.discard(topic_id)
        self._emit("topic_deleted", topic_id=topic_id, name=self._names.get(topic_id, ""))

    def read_messages(self) -> list:
        """Non-blocking harness read before and during model generation."""
        incoming = []
        for topic_id in sorted(self.joined_topics):
            messages = self.communication.receive(topic_id)
            if messages:
                incoming.append({"topic_id": topic_id, "messages": [
                    asdict(message) if is_dataclass(message) else message
                    for message in messages
                ]})
        return incoming
