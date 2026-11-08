"""Exercise the actual WebSocket client against a local protocol fixture."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from websockets.asyncio.server import serve

from sbem_voice.robot.tools import connect_robot


class WebSocketTests(unittest.IsolatedAsyncioTestCase):
    async def test_subscriptions_and_pushed_motion_result_over_real_socket(self):
        received = []

        async def server(socket):
            async for raw in socket:
                message = json.loads(raw)
                received.append(message)
                if message["op"] == "subscribe" and message["topic"] == "/battery_state":
                    await socket.send(
                        json.dumps(
                            {"op": "publish", "topic": "/battery_state", "msg": {"percentage": 0.8}}
                        )
                    )
                if message["op"] == "send_action_goal":
                    await socket.send(
                        json.dumps(
                            {
                                "op": "action_result",
                                "id": message["id"],
                                "result": True,
                                "status": 4,
                                "values": {"success": True},
                            }
                        )
                    )
                if message["op"] == "call_service":
                    await socket.send(
                        json.dumps(
                            {
                                "op": "service_response",
                                "id": message["id"],
                                "result": True,
                                "values": {"success": True, "locations": [{"name": "kitchen"}]},
                            }
                        )
                    )

        async with serve(server, "127.0.0.1", 0) as fixture:
            port = fixture.sockets[0].getsockname()[1]
            with tempfile.TemporaryDirectory() as tmp:
                data = json.loads(Path("config/robot.example.json").read_text())
                data["url"] = f"ws://127.0.0.1:{port}"
                path = Path(tmp) / "robot.json"
                path.write_text(json.dumps(data))
                async with connect_robot(path) as robot:
                    battery = await asyncio.wait_for(robot.get_battery_status(), 2)
                    self.assertEqual(battery, {"percent": 80})
                    self.assertEqual(
                        await asyncio.wait_for(robot.get_positions(), 2), {"positions": ["kitchen"]}
                    )
                    result = await asyncio.wait_for(robot.motion("dock"), 2)
                    self.assertEqual(result, {"status": "succeeded", "operation": "dock"})
        self.assertEqual(
            [m["op"] for m in received], ["subscribe", "call_service", "send_action_goal"]
        )
