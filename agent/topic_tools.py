"""Wrap the topic communication interface as model tools."""

from dataclasses import asdict, is_dataclass

TOOLS = [
    {
        "name": "create_topic",
        "description": "创建 topic，自动加入，返回 topic_id。",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "description": {"type": "string", "default": ""},
            },
            "required": ["name"],
        },
    },
    {
        "name": "discover_topics",
        "description": "返回当前可发现的 topic；没有则返回 []。",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "join_topic",
        "description": "加入 topic，从加入时的末尾开始接收后续消息。",
        "input_schema": {
            "type": "object",
            "properties": {"topic_id": {"type": "string"}},
            "required": ["topic_id"],
        },
    },
    {
        "name": "send",
        "description": "将原始字符串写入指定 topic 的消息通道。",
        "input_schema": {
            "type": "object",
            "properties": {
                "topic_id": {"type": "string"},
                "payload": {"type": "string"},
            },
            "required": ["topic_id", "payload"],
        },
    },
    {
        "name": "leave_topic",
        "description": "退出 topic，停止后续收发。",
        "input_schema": {
            "type": "object",
            "properties": {"topic_id": {"type": "string"}},
            "required": ["topic_id"],
        },
    },
]


class TopicManager:
    def __init__(self, communication):
        self.communication = communication
        self.joined_topics: set[str] = set()
        self.handlers = {
            "create_topic": self.create_topic,
            "discover_topics": self.discover_topics,
            "join_topic": self.join_topic,
            "send": communication.send,
            "leave_topic": self.leave_topic,
        }

    def create_topic(self, name: str, description: str = "") -> str:
        topic_id = self.communication.create_topic(name, description)
        self.joined_topics.add(topic_id)
        return topic_id

    def discover_topics(self) -> list:
        """Turn Comm's TopicInfo dataclasses into JSON-compatible tool output."""
        return [asdict(topic) if is_dataclass(topic) else topic
                for topic in self.communication.discover_topics()]

    def join_topic(self, topic_id: str) -> None:
        self.communication.join_topic(topic_id)
        self.joined_topics.add(topic_id)

    def leave_topic(self, topic_id: str) -> None:
        self.communication.leave_topic(topic_id)
        self.joined_topics.discard(topic_id)

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
