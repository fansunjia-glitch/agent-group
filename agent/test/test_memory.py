"""Memory instances use OpenAI completions without contacting a model service."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agent.memory import (
    CATALOG_CHAR_LIMIT, RECALL_CHAR_LIMIT, MemoryStore, dialogue_text,
    should_store_memory,
)


class FakeClient:
    def __init__(self, responses=()):
        self.responses = iter(responses)
        self.requests = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **request):
        self.requests.append(request)
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=next(self.responses)),
        )])


def fact(**changes):
    return {
        "name": "deployment branch", "description": "Production branch",
        "type": "project", "scope": "persistent", "body": "Use main.",
        **changes,
    }


class MemoryStoreTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="memory-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def store(self, client=None, name="node"):
        return MemoryStore(self.root / name, client or FakeClient(), "deepseek-flash", api_mode="deepseek")

    def test_constructor_has_no_filesystem_or_model_side_effects(self):
        client = FakeClient()
        store = self.store(client)
        self.assertFalse(store.workdir.exists())
        self.assertEqual(client.requests, [])

    def test_correction_updates_same_name_instead_of_discarding_it(self):
        store = self.store()
        self.assertTrue(store.upsert_memory(fact()))
        correction = fact(body="Use release.")
        self.assertTrue(should_store_memory(correction, store.list_memory_files()))
        self.assertTrue(store.upsert_memory(correction))
        self.assertFalse(store.upsert_memory(correction))
        records = store.list_memory_files()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["body"], "Use release.")

    def test_correction_with_same_description_keeps_one_existing_file(self):
        store = self.store()
        store.upsert_memory(fact())
        old_file = store.list_memory_files()[0]["filename"]
        store.upsert_memory(fact(name="release policy", body="Deploy release."))
        records = store.list_memory_files()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["filename"], old_file)
        self.assertEqual(records[0]["name"], "release policy")
        self.assertEqual(records[0]["body"], "Deploy release.")

    def test_instances_do_not_share_memory_or_clients(self):
        first_client = FakeClient([json.dumps([fact()])])
        first = self.store(first_client, "first")
        second = self.store(FakeClient(), "second")
        first.remember_after_turn([{"role": "user", "content": "Remember production branch main."}])
        self.assertEqual(len(first.list_memory_files()), 1)
        self.assertEqual(second.list_memory_files(), [])
        self.assertFalse(second.directory.exists())

    def test_extraction_uses_openai_messages_and_ignores_tool_content(self):
        client = FakeClient([json.dumps([fact()])])
        store = self.store(client)
        messages = [
            {"role": "user", "content": "Production uses main."},
            {"role": "assistant", "content": None, "tool_calls": []},
            {"role": "tool", "tool_call_id": "call-1", "content": "TOOL_ONLY_MARKER"},
            {"role": "assistant", "content": "Understood."},
        ]
        self.assertEqual(store.extract_memories(messages), 1)
        request = client.requests[0]
        self.assertEqual(request["model"], "deepseek-flash")
        self.assertFalse(request["stream"])
        self.assertEqual(request["extra_body"],
                         {"thinking": {"type": "disabled"}, "user_id": store.user_id})
        self.assertNotIn("TOOL_ONLY_MARKER", request["messages"][0]["content"])
        self.assertNotIn("TOOL_ONLY_MARKER", dialogue_text(messages))

    def test_temporary_and_unchanged_facts_are_not_stored(self):
        store = self.store()
        self.assertFalse(store.upsert_memory(fact(scope="current_task")))
        self.assertFalse(store.upsert_memory(fact(body="For now use main.")))
        self.assertTrue(store.upsert_memory(fact()))
        self.assertFalse(store.upsert_memory(fact()))
        self.assertEqual(len(store.list_memory_files()), 1)

    def test_recall_and_system_catalog_are_bounded(self):
        client = FakeClient(["[0]"])
        store = self.store(client)
        store.write_memory_file("large", "project", "large fact", '\\"' * RECALL_CHAR_LIMIT)
        recalled = store.load_memories([{"role": "user", "content": "Find large fact"}])
        self.assertLessEqual(len(recalled), RECALL_CHAR_LIMIT)
        self.assertEqual(json.loads(recalled)[0]["source"], "large.md")
        store.memory_path("MEMORY.md", allow_index=True).write_text(
            "x" * (CATALOG_CHAR_LIMIT * 3), encoding="utf-8",
        )
        system = store.build_system("base", recalled)
        self.assertNotIn("x" * (CATALOG_CHAR_LIMIT + 1), system)
        self.assertLess(len(system), CATALOG_CHAR_LIMIT + RECALL_CHAR_LIMIT + 300)

    def test_invalid_consolidation_keeps_existing_records(self):
        client = FakeClient([json.dumps([{"name": "invalid"}])])
        store = self.store(client)
        store.upsert_memory(fact())
        before = store.list_memory_files()
        with patch("agent.memory.CONSOLIDATE_THRESHOLD", 1):
            with self.assertLogs("agent.memory", level="WARNING"):
                self.assertEqual(store.consolidate_memories(), 0)
        self.assertEqual(store.list_memory_files(), before)

    def test_consolidation_replaces_records_and_rebuilds_index(self):
        updated = fact(body="Use release.")
        client = FakeClient([json.dumps([updated])])
        store = self.store(client)
        store.upsert_memory(fact())
        store.upsert_memory(fact(name="old convention", description="Old convention", body="Obsolete fact."))
        with patch("agent.memory.CONSOLIDATE_THRESHOLD", 2):
            self.assertEqual(store.consolidate_memories(), 1)
        self.assertEqual(len(store.list_memory_files()), 1)
        self.assertEqual(store.list_memory_files()[0]["body"], "Use release.")
        index = store.memory_path("MEMORY.md", allow_index=True).read_text(encoding="utf-8")
        self.assertNotIn("old convention", index)


if __name__ == "__main__":
    unittest.main()
