"""Validated robot interface configuration; example names are not discovered facts."""

import json
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


def field(value, path):
    """Read a dotted object path without evaluating expressions."""
    for key in path.split("."):
        value = value[key]
    return value


def set_field(value, path, item):
    keys = path.split(".")
    for key in keys[:-1]:
        value = value.setdefault(key, {})
    value[keys[-1]] = item


def _text(value):
    return isinstance(value, str) and bool(value.strip())


@dataclass(frozen=True)
class RobotConfig:
    url: str
    frame_id: str
    waypoint_manager: dict
    battery: dict
    actions: dict
    service_timeout_secs: float = 10
    connect_timeout_secs: float = 10
    action_timeout_secs: float = 300
    telemetry_timeout_secs: float = 5
    telemetry_max_age_secs: float = 5
    feedback_interval_secs: float = 15
    feedback_max_updates: int = 3

    @classmethod
    def load(cls, path: Path):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or set(data) - cls.__dataclass_fields__.keys():
                raise ValueError("Unknown robot configuration fields")
            required = ("url", "frame_id", "waypoint_manager", "battery", "actions")
            if any(key not in data for key in required):
                raise ValueError("Missing required robot configuration fields")
            for key in ("url", "frame_id"):
                if not _text(data[key]):
                    raise ValueError(f"{key} must be a nonempty string")

            def expand(match):
                name = match.group(1)
                if not os.getenv(name):
                    raise ValueError(f"Robot configuration requires {name}")
                return os.environ[name]

            data["url"] = re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", expand, data["url"])
            url = urlparse(data["url"])
            if url.scheme not in ("ws", "wss") or not url.hostname:
                raise ValueError("Robot url must use ws or wss")
            limit = data.get("feedback_max_updates", 3)
            if type(limit) is not int or not 0 <= limit <= 100:
                raise ValueError("feedback_max_updates must be an integer from 0 to 100")
            for key in cls.__dataclass_fields__:
                if key.endswith("_secs") and key in data:
                    value = data[key]
                    if (
                        isinstance(value, bool)
                        or not isinstance(value, (float, int))
                        or not math.isfinite(value)
                        or value <= 0
                    ):
                        raise ValueError(f"{key} must be a positive finite number")
            for key, fields in (
                (
                    "battery",
                    {"topic", "type", "percentage_field", "percentage_scale", "voltage_field"},
                ),
            ):
                block = data[key]
                if not isinstance(block, dict) or set(block) != fields:
                    raise ValueError(f"Invalid {key} configuration")
                for name in fields - {"percentage_scale"}:
                    if not _text(block[name]):
                        raise ValueError(f"{key}.{name} must be a nonempty string")
            scale = data["battery"]["percentage_scale"]
            if (
                isinstance(scale, bool)
                or not isinstance(scale, (int, float))
                or not math.isfinite(scale)
                or scale <= 0
            ):
                raise ValueError("battery.percentage_scale must be positive and finite")
            if not isinstance(data["actions"], dict) or set(data["actions"]) != {
                "navigate_to",
                "dock",
                "undock",
            }:
                raise ValueError("actions must define navigate_to, dock, undock")
            for name, action in data["actions"].items():
                required_fields = {"name", "type", "goal", "feedback_fields"}
                if name == "navigate_to":
                    required_fields.add("pose_field")
                if not isinstance(action, dict) or set(action) != required_fields:
                    raise ValueError(f"Invalid action {name}")
                for key in required_fields - {"goal", "feedback_fields"}:
                    if not _text(action[key]):
                        raise ValueError(f"Invalid {name}.{key}")
                if (
                    not isinstance(action["goal"], dict)
                    or not isinstance(action["feedback_fields"], list)
                    or any(not _text(f) for f in action["feedback_fields"])
                ):
                    raise ValueError(f"Invalid goal or feedback_fields for {name}")
            manager = data["waypoint_manager"]
            if not isinstance(manager, dict) or set(manager) != {"map_id", "services"}:
                raise ValueError("waypoint_manager must contain map_id and services")
            if not isinstance(manager["map_id"], str) or not isinstance(manager["services"], dict):
                raise ValueError("Invalid waypoint_manager map_id or services")
            if set(manager["services"]) != {"save_location", "get_location", "list_locations"}:
                raise ValueError("Configure all three waypoint manager services")
            for service in manager["services"].values():
                if (
                    not isinstance(service, dict)
                    or set(service) != {"name", "type"}
                    or any(not _text(value) for value in service.values())
                ):
                    raise ValueError("Waypoint services require nonempty name and type")
            return cls(**data)
        except (OSError, UnicodeError, TypeError, KeyError, json.JSONDecodeError) as exc:
            raise ValueError(f"Cannot read robot configuration: {path}") from exc
