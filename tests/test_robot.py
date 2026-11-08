"""Offline robot behavior using protocol messages; no ROS or robot required."""

import asyncio
import copy
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sbem_voice.robot.bridge import RosbridgeClient
from sbem_voice.robot.config import RobotConfig
from sbem_voice.robot.tools import RobotTools, robot_functions


class Socket:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.sent = asyncio.Queue()
        self.services = []
        self.service_requests = asyncio.Queue()
        self.locations = {}
        self.respond = True
        self.pose = {
            "header": {"frame_id": "map", "stamp": {"sec": 123, "nanosec": 0}},
            "pose": {
                "position": {"x": 1, "y": 2, "z": 0},
                "orientation": {"x": 0, "y": 0, "z": 0, "w": 1},
            },
        }

    async def send(self, message):
        message = json.loads(message)
        if message["op"] != "call_service":
            await self.sent.put(message)
            return
        self.services.append(message)
        await self.service_requests.put(message)
        if not self.respond:
            return
        args = message["args"]
        name = args.get("name")
        if message["service"].endswith("save_location"):
            if name in self.locations:
                values = {"success": False, "message": "Position already exists"}
            else:
                self.locations[name] = {
                    "name": name,
                    "map_id": "map-1",
                    "map_matches_current": True,
                    "pose": self.pose,
                }
                values = {"success": True, "location": self.locations[name]}
        elif message["service"].endswith("list_locations"):
            values = {
                "success": True,
                "map_id": "map-1",
                "locations": list(self.locations.values()),
            }
        elif name in self.locations:
            values = {"success": True, "location": self.locations[name]}
        else:
            values = {"success": False, "message": "Unknown position"}
        await self.incoming.put(
            {
                "op": "service_response",
                "id": message["id"],
                "service": message["service"],
                "result": True,
                "values": values,
            }
        )

    def __aiter__(self):
        return self

    async def __anext__(self):
        message = await self.incoming.get()
        if message is None:
            raise StopAsyncIteration
        return json.dumps(message)

    async def close(self):
        await self.incoming.put(None)


class RobotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        data = json.loads(Path("config/robot.example.json").read_text())
        data["telemetry_timeout_secs"] = 0.05
        data["action_timeout_secs"] = 0.3
        self.path = Path(self.tmp.name) / "robot.json"
        self.path.write_text(json.dumps(data))
        self.config = RobotConfig.load(self.path)
        self.socket = Socket()
        self.connect = patch("sbem_voice.robot.bridge.connect", return_value=self.socket)
        # websockets.connect returns an awaitable connection.
        self.connect.start().side_effect = self.open_socket
        self.addCleanup(self.connect.stop)
        self.bridge = RosbridgeClient(self.config)
        await self.bridge.__aenter__()
        self.addAsyncCleanup(self.bridge.__aexit__, None, None, None)
        self.robot = RobotTools(self.config, self.bridge)
        await self.socket.sent.get()  # battery subscription

    async def open_socket(self, *args, **kwargs):
        return self.socket

    async def publish(self, topic, msg):
        await self.socket.incoming.put({"op": "publish", "topic": topic, "msg": msg})
        await asyncio.sleep(0)

    async def test_store_current_pose_list_and_navigate(self):
        self.assertEqual(
            await self.robot.store_position("kitchen"), {"status": "stored", "name": "kitchen"}
        )
        self.assertEqual(await self.robot.get_positions(), {"positions": ["kitchen"]})
        with self.assertRaisesRegex(ValueError, "already exists"):
            await self.robot.store_position("kitchen")
        task = asyncio.create_task(self.robot.motion("navigate_to", "kitchen"))
        goal = await self.socket.sent.get()
        self.assertEqual(goal["args"]["pose"]["pose"]["position"]["x"], 1)
        self.assertEqual(goal["args"]["pose"]["header"]["frame_id"], "map")
        self.assertEqual(goal["args"]["pose"]["header"]["stamp"]["sec"], 123)
        self.assertEqual(self.socket.services[0]["args"], {"name": "kitchen", "overwrite": False})
        self.assertEqual(self.socket.services[-1]["args"], {"name": "kitchen", "map_id": ""})
        await self.socket.incoming.put(
            {"op": "action_result", "id": goal["id"], "status": 4, "result": True, "values": {}}
        )
        self.assertEqual((await task)["status"], "succeeded")
        self.assertEqual(
            await RobotTools(self.config, self.bridge).get_positions(), {"positions": ["kitchen"]}
        )

    async def test_push_feedback_is_compact_and_final_requires_ros_success(self):
        updates = []

        async def progress(value):
            updates.append(value)

        task = asyncio.create_task(self.robot.motion("dock", progress=progress))
        goal = await self.socket.sent.get()
        for i in range(100):
            await self.socket.incoming.put(
                {
                    "op": "action_feedback",
                    "id": goal["id"],
                    "values": {"state": 2, "huge": "x" * 10000},
                }
            )
        await asyncio.sleep(0.01)
        self.assertEqual(len(updates), 1)
        self.assertNotIn("huge", str(updates))
        await self.socket.incoming.put(
            {"op": "action_result", "id": goal["id"], "status": 6, "result": True, "values": {}}
        )
        self.assertEqual((await task)["status"], "failed")

    async def test_rejects_parallel_motion_and_application_failure(self):
        task = asyncio.create_task(self.robot.motion("undock"))
        goal = await self.socket.sent.get()
        with self.assertRaisesRegex(ValueError, "motion"):
            await self.robot.motion("dock")
        await self.socket.incoming.put(
            {
                "op": "action_result",
                "id": goal["id"],
                "status": 4,
                "result": True,
                "values": {"success": False, "error_code": 3},
            }
        )
        self.assertEqual((await task)["status"], "failed")

    async def test_progress_updates_have_a_configurable_per_action_limit(self):
        updates = []
        changed = asyncio.Queue()

        async def progress(value):
            updates.append(value)
            await changed.put(value)

        self.bridge.config = replace(
            self.config, feedback_interval_secs=0.001, feedback_max_updates=2
        )
        task = asyncio.create_task(self.robot.motion("dock", progress=progress))
        goal = await self.socket.sent.get()
        for state in (1, 2):
            await self.socket.incoming.put(
                {"op": "action_feedback", "id": goal["id"], "values": {"state": state}}
            )
            await asyncio.wait_for(changed.get(), 1)
            await asyncio.sleep(0.005)
        await self.socket.incoming.put(
            {"op": "action_feedback", "id": goal["id"], "values": {"state": 3}}
        )
        await asyncio.sleep(0.005)
        await self.socket.incoming.put(
            {"op": "action_result", "id": goal["id"], "status": 4, "result": True, "values": {}}
        )
        self.assertEqual((await task)["status"], "succeeded")
        self.assertEqual(len(updates), 2)

    async def test_disconnect_settles_motion_and_invalidates_battery(self):
        await self.publish(self.config.battery["topic"], {"percentage": 0.62, "voltage": 25})
        self.assertEqual((await self.robot.get_battery_status())["percent"], 62)
        task = asyncio.create_task(self.robot.motion("dock"))
        await self.socket.sent.get()
        await self.socket.close()
        with self.assertRaisesRegex(ConnectionError, "unknown"):
            await task
        with self.assertRaises(ConnectionError):
            await self.robot.get_battery_status()

    async def test_timeout_requests_cancel_without_reporting_success(self):
        task = asyncio.create_task(self.robot.motion("dock"))
        goal = await self.socket.sent.get()
        with self.assertRaises(TimeoutError):
            await task
        cancel = await self.socket.sent.get()
        self.assertEqual(
            cancel, {"op": "cancel_action_goal", "id": goal["id"], "action": goal["action"]}
        )
        with self.assertRaisesRegex(ValueError, "outcome unknown"):
            await self.robot.motion("undock")

    async def test_final_result_survives_late_feedback_and_disconnect(self):
        task = asyncio.create_task(self.robot.motion("dock"))
        goal = await self.socket.sent.get()
        await self.socket.incoming.put(
            {"op": "action_result", "id": goal["id"], "status": 4, "result": True, "values": {}}
        )
        await self.socket.incoming.put(
            {"op": "action_feedback", "id": goal["id"], "values": {"state": 1}}
        )
        await self.socket.close()
        self.assertEqual((await task)["status"], "succeeded")

    async def test_missing_and_stale_telemetry_are_not_reported_as_current(self):
        with self.assertRaises(TimeoutError):
            await self.robot.get_battery_status()
        await self.publish(self.config.battery["topic"], {"percentage": -1})
        self.assertEqual(await self.robot.get_battery_status(), {"percent": None})
        self.bridge.config = replace(self.config, telemetry_max_age_secs=0.001)
        await asyncio.sleep(0.01)
        with self.assertRaises(TimeoutError):
            await self.robot.get_battery_status()

    async def test_bad_pose_or_unknown_position_never_sends_motion(self):
        await self.robot.store_position("bad")
        self.socket.locations["bad"]["map_matches_current"] = False
        with self.assertRaisesRegex(ValueError, "map"):
            await self.robot.motion("navigate_to", "bad")
        with self.assertRaisesRegex(ValueError, "Unknown"):
            await self.robot.motion("navigate_to", "unknown")
        self.assertTrue(self.socket.sent.empty())

    async def test_service_timeout_does_not_retry_or_send_navigation(self):
        self.socket.respond = False
        self.bridge.config = replace(self.config, service_timeout_secs=0.01)
        with self.assertRaises(TimeoutError):
            await self.robot.motion("navigate_to", "kitchen")
        self.assertEqual(len(self.socket.services), 1)
        self.assertFalse(self.robot.motion_uncertain)
        self.assertTrue(self.socket.sent.empty())

    async def test_service_replies_are_correlated_even_when_reversed(self):
        self.socket.respond = False
        listing = asyncio.create_task(self.robot.get_positions())
        first = await self.socket.service_requests.get()
        saving = asyncio.create_task(self.robot.store_position("hall"))
        second = await self.socket.service_requests.get()
        for request, values in [
            (second, {"success": True}),
            (first, {"success": True, "locations": [{"name": "hall"}]}),
        ]:
            await self.socket.incoming.put(
                {"op": "service_response", "id": request["id"], "result": True, "values": values}
            )
        self.assertEqual(await saving, {"status": "stored", "name": "hall"})
        self.assertEqual(await listing, {"positions": ["hall"]})

    async def test_disconnect_settles_pending_waypoint_service(self):
        self.socket.respond = False
        task = asyncio.create_task(self.robot.get_positions())
        await self.socket.service_requests.get()
        await self.socket.close()
        with self.assertRaisesRegex(ConnectionError, "service outcome unknown"):
            await task

    async def test_save_uses_native_background_policy_and_delivers_delayed_result(self):
        from pipecat.processors.aggregators.llm_context import LLMContext
        from pipecat.services.openrouter.llm import OpenRouterLLMService

        llm = OpenRouterLLMService(api_key="offline-test")
        try:
            llm._sync_registered_tool_handlers(LLMContext(tools=robot_functions).tools)
            self.assertTrue(llm._function_is_async("store_position"))
            self.assertFalse(llm._function_is_async("get_positions"))
        finally:
            await llm._client.close()
        self.socket.respond = False
        results = []

        async def callback(value):
            results.append(value)

        params = SimpleNamespace(app_resources=self.robot, result_callback=callback)
        store = next(fn for fn in robot_functions if fn.__name__ == "store_position")
        task = asyncio.create_task(store(params, "hall"))
        request = await self.socket.service_requests.get()
        self.assertEqual(results, [])
        await self.socket.incoming.put(
            {
                "op": "service_response",
                "id": request["id"],
                "result": True,
                "values": {"success": True},
            }
        )
        await task
        self.assertEqual(results, [{"status": "stored", "name": "hall"}])

    async def test_direct_functions_report_intermediate_and_final_callbacks(self):
        from pipecat.processors.aggregators.llm_context import LLMContext

        context = LLMContext(tools=robot_functions)
        self.assertEqual(len(context.tools.standard_tools), 6)
        updates = []

        async def callback(value, *, properties=None):
            updates.append((value, properties))

        params = SimpleNamespace(app_resources=self.robot, result_callback=callback)
        dock = next(fn for fn in robot_functions if fn.__name__ == "dock")
        task = asyncio.create_task(dock(params))
        goal = await self.socket.sent.get()
        await self.socket.incoming.put(
            {"op": "action_feedback", "id": goal["id"], "values": {"state": 2}}
        )
        await asyncio.sleep(0.01)
        await self.socket.incoming.put(
            {
                "op": "action_result",
                "id": goal["id"],
                "status": 4,
                "result": True,
                "values": {"success": True},
            }
        )
        await task
        self.assertFalse(updates[0][1].is_final)
        self.assertEqual(updates[-1][0]["status"], "succeeded")
        self.assertIsNone(updates[-1][1])


class ConfigTests(unittest.TestCase):
    def test_rejects_invalid_robot_configuration(self):
        original = json.loads(Path("config/robot.example.json").read_text())
        for key, value in [
            ("url", "http://localhost"),
            ("action_timeout_secs", -1),
            ("feedback_interval_secs", 0),
            ("unknown", True),
        ]:
            data = copy.deepcopy(original)
            data[key] = value
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "robot.json"
                path.write_text(json.dumps(data))
                with self.subTest(key=key), self.assertRaises(ValueError):
                    RobotConfig.load(path)
