"""Exercise the actual WebSocket client against a local protocol fixture."""

import asyncio
import base64
import io
import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image
from websockets.asyncio.server import serve

from sbem_voice.robot.tools import connect_robot


class WebSocketTests(unittest.IsolatedAsyncioTestCase):
    async def test_camera_snapshot_larger_than_one_megabyte_over_real_socket(self):
        unsubscribed = asyncio.Event()
        image = {
            "width": 640,
            "height": 480,
            "encoding": "rgb8",
            "step": 640 * 3,
            "is_bigendian": 0,
            "data": base64.b64encode(b"\x80" * (640 * 480 * 3)).decode(),
        }

        async def server(socket):
            async for raw in socket:
                message = json.loads(raw)
                if message["op"] == "subscribe" and message["topic"] == "/camera/image_raw":
                    payload = json.dumps(
                        {
                            "op": "publish",
                            "topic": message["topic"],
                            "msg": image,
                        }
                    )
                    self.assertGreater(len(payload), 2**20)
                    await socket.send(payload)
                elif message["op"] == "unsubscribe":
                    unsubscribed.set()

        async with serve(server, "127.0.0.1", 0) as fixture:
            port = fixture.sockets[0].getsockname()[1]
            with tempfile.TemporaryDirectory() as tmp:
                data = json.loads(Path("config/robot.example.json").read_text())
                data["url"] = f"ws://127.0.0.1:{port}"
                path = Path(tmp) / "robot.json"
                path.write_text(json.dumps(data))
                async with connect_robot(path) as robot:
                    jpeg, size = await asyncio.wait_for(robot.get_camera_image(), 3)
                    self.assertEqual(size, (640, 480))
                    with Image.open(io.BytesIO(jpeg)) as snapshot:
                        self.assertEqual(snapshot.format, "JPEG")
                        self.assertEqual(snapshot.size, size)
                    await asyncio.wait_for(unsubscribed.wait(), 1)

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
                    result = await asyncio.wait_for(robot.cmd_vel(0.1, -0.2, 0.01), 2)
                    self.assertEqual(result, {"status": "command_sent", "operation": "cmd_vel"})
        self.assertEqual(
            [m["op"] for m in received[:3]],
            ["subscribe", "call_service", "send_action_goal"],
        )
        publications = received[3:]
        self.assertGreaterEqual(len(publications), 2)
        self.assertTrue(all(message["op"] == "publish" for message in publications))
        self.assertEqual(received[-2]["msg"]["linear"]["x"], 0.1)
        self.assertEqual(received[-2]["msg"]["angular"]["z"], -0.2)
        self.assertEqual(received[-1]["msg"]["linear"]["x"], 0)
        self.assertEqual(received[-1]["msg"]["angular"]["z"], 0)
