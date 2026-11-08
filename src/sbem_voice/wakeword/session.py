"""Wake/sleep coordination using Pipecat's native inactivity timer."""

import asyncio

from loguru import logger
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    FunctionCallCancelFrame,
    FunctionCallResultFrame,
    FunctionCallsStartedFrame,
    InterruptionFrame,
    LLMFullResponseStartFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameProcessorSetup
from pipecat.turns.user_idle_controller import UserIdleController


class ConversationSession:
    """One native idle controller, plus application wake and busy state.

    Turn hooks come from the user aggregator; response completion comes after
    the output transport so buffered TTS does not consume the listening window.
    """

    def __init__(self, idle_secs: float):
        self.awake = False
        self.generation = 0
        self._idle = UserIdleController(user_idle_timeout=idle_secs)
        self._idle.add_event_handler("on_user_turn_idle", self._on_idle)
        self._lock = asyncio.Lock()
        self._user_speaking = False
        self._bot_speaking = False
        self._awaiting_response = False
        self._response_completed = False
        self._tools: set[str] = set()

    async def setup(self, setup: FrameProcessorSetup):
        await self._idle.setup(setup)

    async def cleanup(self):
        self.awake = False
        await self._idle.cleanup()

    async def wake(self):
        async with self._lock:
            self.awake = True
            logger.info("Wake word detected; conversation open")
            await self._idle.wait_for_user()

    async def sleep(self):
        async with self._lock:
            await self._sleep()

    async def _sleep(self):
        await self._idle.stop()
        self.awake = False
        self.generation += 1
        logger.info("Conversation idle; waiting for wake word")

    @property
    def busy(self):
        return (
            self._user_speaking
            or self._bot_speaking
            or self._awaiting_response
            or bool(self._tools)
        )

    async def _on_idle(self, controller):
        async with self._lock:
            if self.awake and not self.busy:
                await self._sleep()

    async def user_started(self):
        async with self._lock:
            if not self.awake:
                return
            self._user_speaking = True
            self._response_completed = False
            await self._idle.process_frame(UserStartedSpeakingFrame())

    async def user_stopped(self, *, has_text: bool):
        async with self._lock:
            if not self.awake:
                return
            self._user_speaking = False
            self._awaiting_response = has_text and not self._response_completed
            await self._idle.process_frame(UserStoppedSpeakingFrame())
            if not self.busy:
                await self._idle.wait_for_user()

    async def finalize_turn(self, *, has_text: bool):
        # A delayed callback from an earlier turn must not clear current speech.
        async with self._lock:
            if not self.awake or self._user_speaking:
                return
            self._awaiting_response = has_text and not self._response_completed
            if not self.busy:
                await self._idle.wait_for_user()

    async def process_frame(self, frame: Frame):
        async with self._lock:
            if not self.awake:
                return
            if isinstance(frame, BotStartedSpeakingFrame):
                self._bot_speaking = True
            elif isinstance(frame, BotStoppedSpeakingFrame):
                self._bot_speaking = False
            elif isinstance(frame, LLMFullResponseStartFrame):
                self._awaiting_response = True
                self._response_completed = False
            elif isinstance(frame, FunctionCallsStartedFrame):
                self._tools.update(call.tool_call_id for call in frame.function_calls)
                self._awaiting_response = True
            elif isinstance(frame, FunctionCallResultFrame):
                if frame.properties is None or frame.properties.is_final:
                    self._tools.discard(frame.tool_call_id)
            elif isinstance(frame, FunctionCallCancelFrame):
                self._tools.discard(frame.tool_call_id)
            elif isinstance(frame, InterruptionFrame):
                # The interrupted response will not deliver an output end marker.
                self._awaiting_response = False
            else:
                return
            await self._idle.process_frame(frame)
            if self.busy:
                await self._idle.stop()
            else:
                await self._idle.wait_for_user()

    async def response_finished(self):
        async with self._lock:
            if not self.awake:
                return
            self._awaiting_response = False
            self._response_completed = True
            if not self.busy:
                await self._idle.wait_for_user()
