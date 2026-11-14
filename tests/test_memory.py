"""Persistent memory and native tool behavior, without provider calls."""

import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from pipecat.processors.aggregators.llm_context import LLMContext

from sbem_voice.memory.store import MemoryStore
from sbem_voice.memory.tools import (
    memory_functions,
    remember_memory,
    remove_memory,
    retrieve_memory,
    update_memory,
)
from sbem_voice.resources import AppResources
from sbem_voice.robot.tools import robot_functions


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "data/memory.sqlite3"
        self.store = MemoryStore(self.path, "default")

    def test_remember_survives_reopening_and_duplicates_do_not_add_records(self):
        saved = self.store.remember("  I prefer Italian.  ")
        self.assertEqual(saved["status"], "remembered")
        memory = saved["memory"]
        self.assertEqual(memory["content"], "I prefer Italian.")
        self.assertTrue(memory["created_at"])
        reopened = MemoryStore(self.path, "default")
        self.assertEqual(reopened.retrieve()["memories"], [memory])
        duplicate = reopened.remember("I prefer Italian.")
        self.assertEqual(duplicate["status"], "already_exists")
        self.assertEqual(duplicate["memory"]["id"], memory["id"])

    def test_other_user_cannot_retrieve_update_or_remove_records(self):
        record_id = self.store.remember("I like tea")["memory"]["id"]
        other = MemoryStore(self.path, "other")
        self.assertEqual(other.retrieve()["memories"], [])
        self.assertEqual(other.update(record_id, "I like coffee")["status"], "not_found")
        self.assertEqual(other.remove(record_id)["status"], "not_found")
        other.remember("I like tea")
        self.assertEqual(len(self.store.retrieve()["memories"]), 1)

    def test_updates_and_removal_persist_and_unknown_ids_are_not_success(self):
        record_id = self.store.remember("I like tea")["memory"]["id"]
        updated = self.store.update(record_id, "I like coffee")
        self.assertEqual(updated["status"], "updated")
        self.assertEqual(updated["memory"]["content"], "I like coffee")
        self.assertEqual(MemoryStore(self.path, "default").retrieve("tea")["memories"], [])
        self.assertEqual(
            self.store.remove(record_id), {"status": "removed", "memory_id": record_id}
        )
        self.assertEqual(MemoryStore(self.path, "default").retrieve()["memories"], [])
        self.assertEqual(self.store.remove(record_id)["status"], "not_found")
        self.assertEqual(self.store.update(record_id, "hello")["status"], "not_found")

    def test_update_cannot_overwrite_another_existing_memory(self):
        first = self.store.remember("tea")["memory"]["id"]
        self.store.remember("coffee")
        self.assertEqual(self.store.update(first, "coffee")["status"], "already_exists")
        self.assertEqual(len(self.store.retrieve()["memories"]), 2)
        self.assertEqual(self.store.retrieve("tea")["memories"][0]["id"], first)

    def test_search_is_literal_unicode_case_insensitive_and_paginated(self):
        for content in ["CITTÀ preferita: Roma", "100% tea_lover", "I'm Italian", "other"]:
            self.store.remember(content)
        self.assertEqual(len(self.store.retrieve("città")["memories"]), 1)
        self.assertEqual(len(self.store.retrieve("%")["memories"]), 1)
        self.assertEqual(len(self.store.retrieve("_")["memories"]), 1)
        self.assertEqual(self.store.retrieve("' OR 1=1 --")["memories"], [])
        page = self.store.retrieve(limit=2)
        self.assertTrue(page["has_more"])
        next_page = self.store.retrieve(limit=2, offset=2)
        self.assertFalse(next_page["has_more"])
        self.assertEqual(len({m["id"] for m in page["memories"] + next_page["memories"]}), 4)

    def test_invalid_inputs_do_not_change_storage(self):
        for content in ["", "   ", "x" * 2001, None]:
            with self.subTest(content=str(content)[:20]), self.assertRaises(ValueError):
                self.store.remember(content)
        for kwargs in [{"limit": 0}, {"limit": 51}, {"offset": -1}, {"limit": True}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.store.retrieve(**kwargs)
        for record_id in [0, -1, True, "1", 2**80]:
            with self.subTest(record_id=record_id), self.assertRaises(ValueError):
                self.store.remove(record_id)
        with self.assertRaises(ValueError):
            MemoryStore(self.path, " ")
        self.assertEqual(self.store.retrieve()["memories"], [])


class MemoryToolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = MemoryStore(Path(self.tmp.name) / "memory.sqlite3", "default")
        self.results = []

        async def callback(result):
            self.results.append(result)

        self.params = SimpleNamespace(
            app_resources=AppResources(memory=self.store), result_callback=callback
        )

    async def test_tool_roundtrip(self):
        await remember_memory(self.params, "I prefer Italian")
        record_id = self.results[-1]["memory"]["id"]
        await retrieve_memory(self.params, "Italian")
        self.assertEqual(self.results[-1]["memories"][0]["id"], record_id)
        await update_memory(self.params, record_id, "I prefer English")
        self.assertEqual(self.results[-1]["status"], "updated")
        await remove_memory(self.params, record_id)
        self.assertEqual(self.results[-1]["status"], "removed")
        await retrieve_memory(self.params)
        self.assertEqual(self.results[-1]["memories"], [])

    async def test_validation_and_database_errors_return_failure_callbacks(self):
        await remember_memory(self.params, " ")
        self.assertEqual(self.results[-1]["status"], "error")
        with sqlite3.connect(self.store.path) as connection:
            connection.execute("DROP TABLE memories")
        await retrieve_memory(self.params)
        self.assertEqual(self.results[-1]["status"], "error")

    async def test_native_schemas_do_not_let_model_choose_user(self):
        context = LLMContext(tools=memory_functions)
        schemas = [tool.to_default_dict() for tool in context.tools.standard_tools]
        self.assertEqual(
            {schema["name"] for schema in schemas},
            {"retrieve_memory", "remember_memory", "update_memory", "remove_memory"},
        )
        for schema in schemas:
            self.assertNotIn("user_id", schema["parameters"]["properties"])

    async def test_memory_and_robot_tools_register_together_with_mutations_noninterruptible(self):
        from pipecat.services.openrouter.llm import OpenRouterLLMService

        context = LLMContext(tools=memory_functions + robot_functions)
        self.assertEqual(len(context.tools.standard_tools), 13)
        llm = OpenRouterLLMService(api_key="offline-test")
        try:
            llm._sync_registered_tool_handlers(context.tools)
            for name in ["remember_memory", "update_memory", "remove_memory", "navigate_to"]:
                self.assertTrue(llm._function_is_async(name))
            self.assertFalse(llm._function_is_async("retrieve_memory"))
        finally:
            await llm._client.close()
