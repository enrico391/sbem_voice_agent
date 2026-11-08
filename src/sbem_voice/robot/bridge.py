"""One WebSocket receiver, pushed action events, and telemetry outside LLM context."""

import asyncio
import contextlib
import json
import time
import uuid

from websockets.asyncio.client import connect

from sbem_voice.robot.config import RobotConfig


class RosbridgeClient:
    def __init__(self, config: RobotConfig):
        self.config = config
        self.socket = None
        self.reader = None
        self.connected = False
        self.pending = {}
        self.services = {}
        self.cache = {}
        self.changed = {config.battery["topic"]: asyncio.Event()}

    async def __aenter__(self):
        try:
            self.socket = await connect(
                self.config.url,
                open_timeout=self.config.connect_timeout_secs,
                max_size=2**20,
                max_queue=16,
            )
            self.connected = True
            self.reader = asyncio.create_task(self._receive(), name="rosbridge-receiver")
            for block in (self.config.battery,):
                await self.send(
                    {
                        "op": "subscribe",
                        "id": uuid.uuid4().hex,
                        "topic": block["topic"],
                        "type": block["type"],
                        "throttle_rate": 200,
                        "queue_length": 1,
                    }
                )
            return self
        except BaseException:
            await self.__aexit__(None, None, None)
            raise

    async def __aexit__(self, *args):
        if self.socket:
            for call_id, (action, _) in list(self.pending.items()):
                with contextlib.suppress(Exception):
                    await self.send({"op": "cancel_action_goal", "id": call_id, "action": action})
            await self.socket.close()
        if self.reader:
            self.reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.reader
        self.connected = False
        self.cache.clear()

    async def send(self, message):
        if not self.connected:
            raise ConnectionError("Rosbridge disconnected; action outcome may be unknown")
        try:
            await self.socket.send(json.dumps(message, allow_nan=False))
        except Exception as exc:
            raise ConnectionError("Rosbridge disconnected; action outcome may be unknown") from exc

    @staticmethod
    def _offer(queue, message):
        # Keep only the newest feedback; a final result always replaces feedback.
        if queue.full():
            previous = queue.get_nowait()
            if previous["op"] != "action_feedback":
                queue.put_nowait(previous)
                return
        queue.put_nowait(message)

    async def _receive(self):
        try:
            async for raw in self.socket:
                message = json.loads(raw)
                if not isinstance(message, dict):
                    continue
                op = message.get("op")
                if op == "publish" and message.get("topic") in self.changed:
                    topic = message["topic"]
                    self.cache[topic] = (time.monotonic(), message.get("msg", {}))
                    self.changed[topic].set()
                elif message.get("id") in self.pending:
                    if op in ("action_feedback", "action_result") or (
                        op == "status" and message.get("level") == "error"
                    ):
                        self._offer(self.pending[message["id"]][1], message)
                elif message.get("id") in self.services:
                    if op == "service_response" or (
                        op == "status" and message.get("level") == "error"
                    ):
                        future = self.services[message["id"]]
                        if not future.done():
                            future.set_result(message)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Do not expose connection URLs/tokens or raw protocol bodies.
            pass
        finally:
            self.connected = False
            self.cache.clear()
            for _, queue in self.pending.values():
                self._offer(queue, {"op": "disconnected"})
            for event in self.changed.values():
                event.set()
            for future in self.services.values():
                if not future.done():
                    future.set_result({"op": "disconnected"})

    async def call_service(self, service, arguments):
        """Correlate one service reply; never retry a request with an uncertain outcome."""
        call_id = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.services[call_id] = future
        try:
            async with asyncio.timeout(self.config.service_timeout_secs):
                await self.send(
                    {
                        "op": "call_service",
                        "id": call_id,
                        "service": service["name"],
                        "type": service["type"],
                        "args": arguments,
                    }
                )
                message = await future
            if message["op"] == "disconnected":
                raise ConnectionError("Rosbridge disconnected; service outcome unknown")
            if message["op"] != "service_response" or message.get("result") is not True:
                raise RuntimeError("Rosbridge service request failed")
            values = message.get("values")
            if not isinstance(values, dict):
                raise ValueError("Invalid waypoint service response")
            return values
        finally:
            self.services.pop(call_id, None)

    async def telemetry(self, topic):
        async with asyncio.timeout(self.config.telemetry_timeout_secs):
            while True:
                if not self.connected:
                    raise ConnectionError("Rosbridge disconnected; no current telemetry")
                cached = self.cache.get(topic)
                if cached and time.monotonic() - cached[0] <= self.config.telemetry_max_age_secs:
                    return cached[1]
                self.changed[topic].clear()
                await self.changed[topic].wait()

    async def cancel_action(self, action_name):
        """Request cancellation of a pending goal; its action still owns the result."""
        for call_id, (name, _) in list(self.pending.items()):
            if name == action_name:
                await self.send({"op": "cancel_action_goal", "id": call_id, "action": name})
                return True
        return False

    async def action(self, action, goal, progress=None):
        call_id = uuid.uuid4().hex
        queue = asyncio.Queue(maxsize=1)
        self.pending[call_id] = (action["name"], queue)
        finished = False
        last_feedback = -float("inf")
        feedback_count = 0
        try:
            async with asyncio.timeout(self.config.action_timeout_secs):
                await self.send(
                    {
                        "op": "send_action_goal",
                        "id": call_id,
                        "action": action["name"],
                        "action_type": action["type"],
                        "args": goal,
                        "feedback": bool(progress),
                    }
                )
                while True:
                    message = await queue.get()
                    op = message["op"]
                    if op == "action_result":
                        finished = True
                        return message
                    if op == "disconnected":
                        raise ConnectionError("Rosbridge disconnected; action outcome unknown")
                    if op == "status":
                        raise RuntimeError("Rosbridge rejected action; outcome unknown")
                    now = time.monotonic()
                    if (
                        progress
                        and feedback_count < self.config.feedback_max_updates
                        and now - last_feedback >= self.config.feedback_interval_secs
                    ):
                        last_feedback = now
                        feedback_count += 1
                        await progress(message.get("values", {}))
        finally:
            if not finished and self.connected:
                with contextlib.suppress(Exception):
                    await self.send(
                        {"op": "cancel_action_goal", "id": call_id, "action": action["name"]}
                    )
            self.pending.pop(call_id, None)
