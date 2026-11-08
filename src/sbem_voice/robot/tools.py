"""Robot LLM tools; ROS messages and state stay behind the tool boundary."""

import asyncio
import copy
import math
from contextlib import asynccontextmanager

from pipecat.adapters.schemas.direct_function import tool_options
from pipecat.frames.frames import FunctionCallResultProperties
from pipecat.services.llm_service import FunctionCallParams

from sbem_voice.robot.bridge import RosbridgeClient
from sbem_voice.robot.config import RobotConfig, field, set_field


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _pose(value, frame_id):
    if value["header"]["frame_id"] != frame_id:
        raise ValueError("Position must use the configured map frame")
    pose = value["pose"]
    coordinates = [pose["position"][k] for k in ("x", "y", "z")]
    quaternion = [pose["orientation"][k] for k in ("x", "y", "z", "w")]
    if not all(_number(x) for x in coordinates + quaternion) or not math.isclose(
        sum(x * x for x in quaternion), 1, abs_tol=0.01
    ):
        raise ValueError("Position needs finite coordinates and a unit quaternion")
    return {
        "header": copy.deepcopy(value["header"]),
        "pose": {
            "position": dict(zip(("x", "y", "z"), coordinates, strict=True)),
            "orientation": dict(zip(("x", "y", "z", "w"), quaternion, strict=True)),
        },
    }


class RobotTools:
    def __init__(self, config: RobotConfig, bridge: RosbridgeClient):
        self.config = config
        self.bridge = bridge
        self.motion_lock = asyncio.Lock()
        self.motion_uncertain = False
        self.current_motion = None
        self.navigation_cancel_requested = False

    @staticmethod
    def _name(name):
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80:
            raise ValueError("Position name must contain 1 to 80 characters")
        return name.strip()

    async def _waypoint(self, operation, arguments):
        service = self.config.waypoint_manager["services"][operation]
        values = await self.bridge.call_service(service, arguments)
        if values.get("success") is not True:
            message = values.get("message", "Waypoint manager rejected the request")
            raise ValueError(str(message)[:300])
        return values

    async def get_positions(self):
        values = await self._waypoint(
            "list_locations", {"map_id": self.config.waypoint_manager["map_id"]}
        )
        locations = values.get("locations")
        if not isinstance(locations, list) or any(
            not isinstance(item, dict) or not isinstance(item.get("name"), str)
            for item in locations
        ):
            raise ValueError("Invalid waypoint list response")
        return {"positions": sorted(item["name"] for item in locations)}

    async def store_position(self, name):
        name = self._name(name)
        await self._waypoint("save_location", {"name": name, "overwrite": False})
        return {"status": "stored", "name": name}

    async def _navigation_pose(self, name):
        values = await self._waypoint(
            "get_location",
            {"name": self._name(name), "map_id": self.config.waypoint_manager["map_id"]},
        )
        try:
            location = values["location"]
            if location["map_matches_current"] is not True:
                raise ValueError("Saved position does not match the current map")
            return _pose(location["pose"], self.config.frame_id)
        except (KeyError, TypeError) as exc:
            raise ValueError("Invalid waypoint pose response") from exc

    async def get_battery_status(self):
        message = await self.bridge.telemetry(self.config.battery["topic"])
        try:
            raw = field(message, self.config.battery["percentage_field"])
        except (KeyError, TypeError):
            raw = None
        percent = raw * self.config.battery["percentage_scale"] if _number(raw) else None
        result = {
            "percent": round(percent, 1) if percent is not None and 0 <= percent <= 100 else None
        }
        try:
            voltage = field(message, self.config.battery["voltage_field"])
            if _number(voltage) and voltage > 0:
                result["voltage"] = round(voltage, 2)
        except (KeyError, TypeError):
            pass
        return result

    async def cancel_navigation(self):
        if self.current_motion != "navigate_to":
            return {"status": "no_active_navigation"}
        self.navigation_cancel_requested = True
        await self.bridge.cancel_action(self.config.actions["navigate_to"]["name"])
        return {"status": "cancellation_requested", "operation": "navigate_to"}

    async def motion(self, operation, position_name=None, progress=None):
        if self.motion_uncertain:
            raise ValueError(
                "Previous motion outcome unknown; check robot and restart before moving"
            )
        if self.motion_lock.locked():
            raise ValueError("Another motion is in progress")
        action = self.config.actions[operation]
        goal = copy.deepcopy(action["goal"])

        async def update(values):
            compact = {"status": "in_progress", "operation": operation}
            for name in action["feedback_fields"]:
                try:
                    value = field(values, name)
                    if _number(value):
                        compact[name] = round(value, 2)
                except (KeyError, TypeError):
                    pass
            if progress:
                await progress(compact)

        async with self.motion_lock:
            self.current_motion = operation
            self.navigation_cancel_requested = False
            try:
                if operation == "navigate_to":
                    pose = await self._navigation_pose(position_name)
                    if self.navigation_cancel_requested:
                        return {"status": "canceled", "operation": operation}
                    set_field(goal, action["pose_field"], pose)
                try:
                    message = await self.bridge.action(action, goal, update if progress else None)
                except (TimeoutError, ConnectionError, RuntimeError, asyncio.CancelledError):
                    self.motion_uncertain = True
                    raise
            finally:
                self.current_motion = None
                self.navigation_cancel_requested = False
        values = message.get("values", {})
        succeeded = message.get("result") is True and message.get("status") == 4
        if isinstance(values, dict):
            succeeded = (
                succeeded
                and values.get("success", True) is not False
                and values.get("error_code", 0) == 0
            )
        status = "succeeded" if succeeded else "failed"
        if message.get("result") is True and message.get("status") == 5:
            status = "canceled"
        result = {"status": status, "operation": operation}
        if isinstance(values, dict) and isinstance(values.get("error_code"), int):
            result["error_code"] = values["error_code"]
        return result


async def _invoke(params, operation, *args):
    try:
        if operation in ("navigate_to", "dock", "undock"):

            async def progress(result):
                await params.result_callback(
                    result, properties=FunctionCallResultProperties(is_final=False)
                )

            result = await params.app_resources.motion(operation, *args, progress=progress)
        else:
            result = await getattr(params.app_resources, operation)(*args)
    except TimeoutError:
        result = {
            "status": "error",
            "reason": (
                "Timed out. Motion outcome may be unknown; a cancellation request "
                "does not confirm the robot stopped."
                if operation in ("navigate_to", "dock", "undock")
                else "Waypoint or telemetry request timed out; do not assume it succeeded."
            ),
        }
    except (ValueError, ConnectionError, RuntimeError) as exc:
        result = {"status": "error", "reason": str(exc)}
    except OSError:
        result = {
            "status": "error",
            "reason": "Robot connection failed; request outcome may be unknown",
        }
    await params.result_callback(result)


@tool_options(cancel_on_interruption=False)
async def navigate_to(params: FunctionCallParams, position_name: str):
    """Navigate to a saved position and report completion.

    Args:
        position_name: Name returned by get_positions.
    """
    await _invoke(params, "navigate_to", position_name)


@tool_options(cancel_on_interruption=False)
async def cancel_navigation(params: FunctionCallParams):
    """Request cancellation of the current navigation, if any.

    A cancellation request does not confirm the robot stopped. The navigation
    tool reports the final outcome when the robot confirms it.
    """
    await _invoke(params, "cancel_navigation")


@tool_options(cancel_on_interruption=False)
async def dock(params: FunctionCallParams):
    """Dock at the configured charging station and report completion."""
    await _invoke(params, "dock")


@tool_options(cancel_on_interruption=False)
async def undock(params: FunctionCallParams):
    """Leave the charging station and report completion."""
    await _invoke(params, "undock")


async def get_positions(params: FunctionCallParams):
    """List saved position names available for navigation."""
    await _invoke(params, "get_positions")


@tool_options(cancel_on_interruption=False)
async def store_position(params: FunctionCallParams, name: str):
    """Save the robot's current map position under a new name.

    Args:
        name: A unique name for the current place.
    """
    await _invoke(params, "store_position", name)


async def get_battery_status(params: FunctionCallParams):
    """Read fresh battery percentage and voltage, when available."""
    await _invoke(params, "get_battery_status")


robot_functions = [
    navigate_to,
    cancel_navigation,
    dock,
    get_positions,
    store_position,
    undock,
    get_battery_status,
]


@asynccontextmanager
async def connect_robot(path):
    if path is None:
        yield None
        return
    config = RobotConfig.load(path)
    async with RosbridgeClient(config) as bridge:
        yield RobotTools(config, bridge)
