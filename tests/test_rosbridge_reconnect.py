"""Recover the shared robot client across real local WebSocket server restarts."""

import asyncio
import json
import socket
import tempfile
import unittest
from contextlib import AsyncExitStack
from pathlib import Path
from unittest.mock import patch

from websockets.asyncio.server import serve

from sbem_voice.robot.config import RobotConfig
from sbem_voice.robot.tools import connect_robot


class ReconnectTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        with socket.socket() as available:
            available.bind(("127.0.0.1", 0))
            self.port = available.getsockname()[1]
        data = json.loads(Path("config/robot.example.json").read_text())
        data.update(
            url=f"ws://127.0.0.1:{self.port}",
            connect_timeout_secs=0.2,
            reconnect_interval_secs=0.03,
            telemetry_timeout_secs=0.3,
            action_timeout_secs=2,
            service_timeout_secs=0.5,
        )
        self.path = Path(self.tmp.name) / "robot.json"
        self.path.write_text(json.dumps(data))

    async def wait_connected(self, robot, value=True):
        async with asyncio.timeout(2):
            while robot.bridge.connected is not value:
                await asyncio.sleep(0.005)

    async def test_agent_resources_exist_when_rosbridge_starts_later(self):
        subscribed = asyncio.Event()

        async def server(ws):
            async for raw in ws:
                message = json.loads(raw)
                if message["op"] == "subscribe":
                    subscribed.set()
                    await ws.send(
                        json.dumps(
                            {
                                "op": "publish",
                                "topic": "/battery_state",
                                "msg": {"percentage": 0.73},
                            }
                        )
                    )
                elif message["op"] == "call_service":
                    await ws.send(
                        json.dumps(
                            {
                                "op": "service_response",
                                "id": message["id"],
                                "result": True,
                                "values": {"success": True, "locations": [{"name": "hall"}]},
                            }
                        )
                    )

        async with connect_robot(self.path) as robot:
            self.assertFalse(robot.bridge.connected)
            with self.assertRaises(ConnectionError):
                await robot.get_positions()
            async with serve(server, "127.0.0.1", self.port):
                await asyncio.wait_for(subscribed.wait(), 2)
                await self.wait_connected(robot)
                self.assertEqual(await robot.get_battery_status(), {"percent": 73})
                self.assertEqual(await robot.get_positions(), {"positions": ["hall"]})

    async def test_restart_resubscribes_discards_stale_battery_and_keeps_same_tools(self):
        connections = []
        requests = []

        async def server(ws):
            connections.append(ws)
            async for raw in ws:
                message = json.loads(raw)
                requests.append(message)
                if message["op"] == "subscribe" and len(connections) == 1:
                    await ws.send(
                        json.dumps(
                            {
                                "op": "publish",
                                "topic": "/battery_state",
                                "msg": {"percentage": 0.8},
                            }
                        )
                    )
                elif message["op"] == "call_service":
                    await ws.send(
                        json.dumps(
                            {
                                "op": "service_response",
                                "id": message["id"],
                                "result": True,
                                "values": {"success": True, "locations": []},
                            }
                        )
                    )

        async with AsyncExitStack() as stack:
            first = await stack.enter_async_context(serve(server, "127.0.0.1", self.port))
            robot = await stack.enter_async_context(connect_robot(self.path))
            self.assertEqual(await robot.get_battery_status(), {"percent": 80})
            first.close()
            await first.wait_closed()
            await self.wait_connected(robot, False)
            await stack.enter_async_context(serve(server, "127.0.0.1", self.port))
            await self.wait_connected(robot)
            self.assertEqual(len(connections), 2)
            self.assertNotIn("/battery_state", robot.bridge.cache)
            await connections[-1].send(
                json.dumps(
                    {
                        "op": "publish",
                        "topic": "/battery_state",
                        "msg": {"percentage": 0.6},
                    }
                )
            )
            self.assertEqual(await robot.get_battery_status(), {"percent": 60})
            self.assertEqual(await robot.get_positions(), {"positions": []})
            self.assertEqual(sum(request["op"] == "subscribe" for request in requests), 2)

    async def test_lost_action_is_not_replayed_and_uncertainty_survives_reconnect(self):
        requests = []
        goal_sent = asyncio.Event()

        async def server(ws):
            async for raw in ws:
                message = json.loads(raw)
                requests.append(message)
                if message["op"] == "send_action_goal":
                    goal_sent.set()  # The server never reports its outcome.

        async with AsyncExitStack() as stack:
            first = await stack.enter_async_context(serve(server, "127.0.0.1", self.port))
            robot = await stack.enter_async_context(connect_robot(self.path))
            motion = asyncio.create_task(robot.motion("dock"))
            await asyncio.wait_for(goal_sent.wait(), 1)
            first.close()
            await first.wait_closed()
            with self.assertRaisesRegex(ConnectionError, "unknown"):
                await asyncio.wait_for(motion, 1)
            await stack.enter_async_context(serve(server, "127.0.0.1", self.port))
            await self.wait_connected(robot)
            self.assertTrue(robot.motion_uncertain)
            with self.assertRaisesRegex(ValueError, "outcome unknown"):
                await robot.motion("dock")
            self.assertEqual(sum(item["op"] == "send_action_goal" for item in requests), 1)
            self.assertEqual(sum(item["op"] == "cancel_action_goal" for item in requests), 0)

    async def test_manual_reconnect_wakes_retry_and_shutdown_stops_retrying(self):
        data = json.loads(self.path.read_text())
        data["reconnect_interval_secs"] = 30
        self.path.write_text(json.dumps(data))
        connected = asyncio.Event()

        async def server(ws):
            connected.set()
            await ws.wait_closed()

        async with connect_robot(self.path) as robot:
            self.assertFalse(robot.bridge.connected)
            async with serve(server, "127.0.0.1", self.port):
                self.assertTrue(await robot.bridge.reconnect())
                self.assertTrue(connected.is_set())
                original = robot.bridge.socket
                self.assertTrue(await robot.bridge.reconnect())
                self.assertIs(robot.bridge.socket, original)
        self.assertFalse(robot.bridge.connected)
        self.assertFalse(await robot.bridge.reconnect())

    async def test_velocity_stream_does_not_resume_on_replacement_connection(self):
        data = json.loads(self.path.read_text())
        data["cmd_vel"]["publish_rate_hz"] = 2
        self.path.write_text(json.dumps(data))
        first_command = asyncio.Event()
        connections = []
        requests = []

        async def server(ws):
            connections.append(ws)
            async for raw in ws:
                message = json.loads(raw)
                requests.append((ws, message))
                if message["op"] == "publish":
                    first_command.set()

        async with AsyncExitStack() as stack:
            first = await stack.enter_async_context(serve(server, "127.0.0.1", self.port))
            robot = await stack.enter_async_context(connect_robot(self.path))
            command = asyncio.create_task(robot.cmd_vel(0.1, 0, 1))
            try:
                await asyncio.wait_for(first_command.wait(), 1)
                first.close()
                await first.wait_closed()
                await self.wait_connected(robot, False)
                await stack.enter_async_context(serve(server, "127.0.0.1", self.port))
                await self.wait_connected(robot)
                with self.assertRaises(ConnectionError):
                    await asyncio.wait_for(command, 2)
                self.assertTrue(robot.motion_uncertain)
                self.assertFalse(
                    any(
                        owner is connections[-1] and message["op"] == "publish"
                        for owner, message in requests
                    )
                )
            finally:
                command.cancel()
                await asyncio.gather(command, return_exceptions=True)

    async def test_late_send_failure_on_old_socket_cannot_invalidate_recovered_connection(self):
        send_started = asyncio.Event()
        release = asyncio.Event()

        async def server(ws):
            await ws.wait_closed()

        async def blocked_send(raw):
            send_started.set()
            await release.wait()
            raise OSError("old socket failed")

        async with AsyncExitStack() as stack:
            first = await stack.enter_async_context(serve(server, "127.0.0.1", self.port))
            robot = await stack.enter_async_context(connect_robot(self.path))
            old_socket = robot.bridge.socket
            with patch.object(old_socket, "send", side_effect=blocked_send):
                request = asyncio.create_task(robot.get_positions())
                try:
                    await asyncio.wait_for(send_started.wait(), 1)
                    first.close()
                    await first.wait_closed()
                    await self.wait_connected(robot, False)
                    await stack.enter_async_context(serve(server, "127.0.0.1", self.port))
                    await self.wait_connected(robot)
                    new_socket = robot.bridge.socket
                    release.set()
                    with self.assertRaises(ConnectionError):
                        await request
                    self.assertTrue(robot.bridge.connected)
                    self.assertIs(robot.bridge.socket, new_socket)
                finally:
                    release.set()
                    await asyncio.gather(request, return_exceptions=True)

    async def test_invalid_retry_interval_is_rejected(self):
        data = json.loads(self.path.read_text())
        for value in (0, -1, True, "soon", float("inf")):
            with self.subTest(value=value):
                data["reconnect_interval_secs"] = value
                self.path.write_text(json.dumps(data))
                with self.assertRaisesRegex(ValueError, "reconnect_interval_secs"):
                    RobotConfig.load(self.path)
