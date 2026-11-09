"""Application-owned resources shared by native tool handlers."""

from dataclasses import dataclass

from sbem_voice.memory.store import MemoryStore
from sbem_voice.robot.tools import RobotTools
from sbem_voice.wakeword.session import ConversationSession


@dataclass(frozen=True)
class AppResources:
    memory: MemoryStore
    robot: RobotTools | None = None
    session: ConversationSession | None = None
