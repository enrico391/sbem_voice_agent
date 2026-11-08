"""Mute speaker echo before speech recognition and context aggregation."""

import math
from time import monotonic

from pipecat.frames.frames import BotStoppedSpeakingFrame, Frame, STTMuteFrame
from pipecat.services.stt_service import STTService
from pipecat.turns.user_mute import AlwaysUserMuteStrategy


class SpeakerMuteStrategy(AlwaysUserMuteStrategy):
    """Mute recognition while the bot speaks and while its echo fades."""

    def __init__(self, stt: STTService, echo_tail_secs: float = 0.8):
        super().__init__()
        if not math.isfinite(echo_tail_secs) or echo_tail_secs < 0:
            raise ValueError("echo_tail_secs must be a finite, non-negative number")
        self._stt = stt
        self._echo_tail_secs = echo_tail_secs
        self._unmute_at = 0.0
        self._stt_muted = False

    async def process_frame(self, frame: Frame) -> bool:
        bot_speaking = await super().process_frame(frame)
        if isinstance(frame, BotStoppedSpeakingFrame):
            self._unmute_at = monotonic() + self._echo_tail_secs

        muted = bot_speaking or monotonic() < self._unmute_at
        if muted != self._stt_muted:
            self._stt_muted = muted
            # Aggregator muting alone cannot stop audio reaching an upstream STT.
            await self._stt.queue_frame(STTMuteFrame(mute=muted))
        return muted
