"""One WebSocket receiver, pushed action events, and telemetry outside LLM context."""

import asyncio
import contextlib
import json
import time
import uuid

from loguru import logger
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
        self._camera_lock = asyncio.Lock()
        self._camera_pending = None
        self._supervisor = None
        self._closing = False
        self._initial_attempt_done = asyncio.Event()
        self._ready = asyncio.Event()
        self._retry_now = asyncio.Event()

    async def __aenter__(self):
        self._closing = False
        self._initial_attempt_done.clear()
        self._supervisor = asyncio.create_task(self._run(), name="rosbridge-connection")
        try:
            # One bounded attempt preserves fast startup when available, but an
            # offline robot must not prevent the voice/API pipeline from starting.
            await self._initial_attempt_done.wait()
            return self
        except BaseException:
            await self.__aexit__(None, None, None)
            raise

    async def __aexit__(self, *args):
        self._closing = True
        self._retry_now.set()
        socket = self.socket
        if socket is not None and self.connected:
            for call_id, (action, _, owner) in list(self.pending.items()):
                if owner is not socket:
                    continue
                with contextlib.suppress(Exception):
                    async with asyncio.timeout(self.config.connect_timeout_secs):
                        await self.send(
                            {"op": "cancel_action_goal", "id": call_id, "action": action},
                            expected_socket=socket,
                        )
        if self._supervisor is not None:
            self._supervisor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._supervisor
            self._supervisor = None
        self._invalidate()

    async def _run(self):
        outage_logged = False
        while not self._closing:
            self._retry_now.clear()
            socket = None
            try:
                async with asyncio.timeout(self.config.connect_timeout_secs):
                    socket = await connect(
                        self.config.url,
                        open_timeout=self.config.connect_timeout_secs,
                        close_timeout=1,
                        # Raw camera images travel as base64; allow full HD color.
                        max_size=2**24,
                        max_queue=16,
                    )
                    self.socket = socket
                    block = self.config.battery
                    await socket.send(
                        json.dumps(
                            {
                                "op": "subscribe",
                                "id": uuid.uuid4().hex,
                                "topic": block["topic"],
                                "type": block["type"],
                                "throttle_rate": 200,
                                "queue_length": 1,
                            }
                        )
                    )
                self.connected = True
                self._ready.set()
                self.reader = asyncio.create_task(self._receive(socket), name="rosbridge-receiver")
                self._initial_attempt_done.set()
                outage_logged = False
                logger.info("Rosbridge connected; robot tools available")
                await self.reader
            except asyncio.CancelledError:
                raise
            except Exception:
                # URLs may contain credentials; never log their exceptions or bodies.
                pass
            finally:
                self._invalidate()
                self._initial_attempt_done.set()
                if socket is not None:
                    with contextlib.suppress(Exception):
                        async with asyncio.timeout(self.config.connect_timeout_secs):
                            await socket.close()
                self.socket = None
                self.reader = None
            if not self._closing:
                if not outage_logged:
                    logger.warning("Rosbridge unavailable; reconnecting in the background")
                    outage_logged = True
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._retry_now.wait(), self.config.reconnect_interval_secs
                    )

    async def reconnect(self):
        """Wake one shared retry loop; never replace a healthy connection."""
        if self._closing or self._supervisor is None:
            return False
        if self.connected:
            return True
        self._retry_now.set()
        try:
            await asyncio.wait_for(self._ready.wait(), self.config.connect_timeout_secs)
        except TimeoutError:
            return False
        return self.connected

    async def send(self, message, *, expected_socket=None):
        socket = self.socket
        if (
            not self.connected
            or socket is None
            or (expected_socket is not None and expected_socket is not socket)
        ):
            raise ConnectionError("Rosbridge disconnected; action outcome may be unknown")
        encoded = json.dumps(message, allow_nan=False)
        try:
            await socket.send(encoded)
        except Exception as exc:
            # A delayed send on an old socket can finish after recovery.
            if self.socket is socket:
                self._invalidate()
                self._retry_now.set()
            with contextlib.suppress(Exception):
                async with asyncio.timeout(self.config.connect_timeout_secs):
                    await socket.close()
            raise ConnectionError("Rosbridge disconnected; action outcome may be unknown") from exc

    async def publish_velocity(self, linear_x, angular_z, *, expected_socket=None):
        """Publish an unlatched planar Twist; sending is not a motion acknowledgment."""
        async with asyncio.timeout(self.config.connect_timeout_secs):
            await self.send(
                {
                    "op": "publish",
                    "topic": self.config.cmd_vel["topic"],
                    "type": "geometry_msgs/msg/Twist",
                    "latch": False,
                    "queue_size": 1,
                    "msg": {
                        "linear": {"x": linear_x, "y": 0.0, "z": 0.0},
                        "angular": {"x": 0.0, "y": 0.0, "z": angular_z},
                    },
                },
                expected_socket=expected_socket,
            )

    @staticmethod
    def _offer(queue, message):
        # Keep only the newest feedback; a final result always replaces feedback.
        if queue.full():
            previous = queue.get_nowait()
            if previous["op"] != "action_feedback":
                queue.put_nowait(previous)
                return
        queue.put_nowait(message)

    async def _receive(self, socket):
        try:
            async for raw in socket:
                message = json.loads(raw)
                if not isinstance(message, dict):
                    continue
                op = message.get("op")
                if self._camera_pending and (
                    (op == "publish" and message.get("topic") == self.config.camera["topic"])
                    or (
                        op == "status"
                        and message.get("id") == self._camera_pending[0]
                        and message.get("level") == "error"
                    )
                ):
                    future = self._camera_pending[1]
                    if not future.done():
                        future.set_result(message)
                elif op == "publish" and message.get("topic") in self.changed:
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
            self._invalidate()

    def _invalidate(self):
        self.connected = False
        self._ready.clear()
        self.cache.clear()
        for _, queue, _ in self.pending.values():
            self._offer(queue, {"op": "disconnected"})
        for event in self.changed.values():
            event.set()
        for future in self.services.values():
            if not future.done():
                future.set_result({"op": "disconnected"})
        if self._camera_pending and not self._camera_pending[1].done():
            self._camera_pending[1].set_result({"op": "disconnected"})

    async def camera_snapshot(self):
        """Subscribe on demand for one newly received frame, then unsubscribe."""
        camera = self.config.camera
        async with asyncio.timeout(camera["timeout_secs"]), self._camera_lock:
            socket = self.socket
            request_id = uuid.uuid4().hex
            future = asyncio.get_running_loop().create_future()
            self._camera_pending = (request_id, future)
            try:
                await self.send(
                    {
                        "op": "subscribe",
                        "id": request_id,
                        "topic": camera["topic"],
                        "type": "sensor_msgs/msg/Image",
                        "throttle_rate": 1000,
                        "queue_length": 1,
                    },
                    expected_socket=socket,
                )
                message = await future
                if message["op"] == "disconnected":
                    raise ConnectionError("Rosbridge disconnected; camera image unavailable")
                if message["op"] == "status":
                    raise RuntimeError("Camera subscription rejected")
                return message.get("msg")
            finally:
                self._camera_pending = None
                if not future.done():
                    future.cancel()
                if self.connected and self.socket is socket:
                    async with asyncio.timeout(self.config.connect_timeout_secs):
                        await self.send(
                            {"op": "unsubscribe", "id": request_id, "topic": camera["topic"]},
                            expected_socket=socket,
                        )

    async def call_service(self, service, arguments):
        """Correlate one service reply; never retry a request with an uncertain outcome."""
        call_id = uuid.uuid4().hex
        socket = self.socket
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
                    },
                    expected_socket=socket,
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
        for call_id, (name, _, socket) in list(self.pending.items()):
            if name == action_name:
                await self.send(
                    {"op": "cancel_action_goal", "id": call_id, "action": name},
                    expected_socket=socket,
                )
                return True
        return False

    async def action(self, action, goal, progress=None, *, expected_socket=None):
        call_id = uuid.uuid4().hex
        socket = self.socket if expected_socket is None else expected_socket
        queue = asyncio.Queue(maxsize=1)
        self.pending[call_id] = (action["name"], queue, socket)
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
                    },
                    expected_socket=socket,
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
            if not finished and self.connected and self.socket is socket:
                with contextlib.suppress(Exception):
                    await self.send(
                        {"op": "cancel_action_goal", "id": call_id, "action": action["name"]},
                        expected_socket=socket,
                    )
            self.pending.pop(call_id, None)
