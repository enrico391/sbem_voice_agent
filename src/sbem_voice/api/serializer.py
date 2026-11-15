"""Carry native RTVI messages as JSON over Pipecat's WebSocket transport."""

import json

import pipecat.processors.frameworks.rtvi.models as RTVI
from pipecat.frames.frames import (
    InputTransportMessageFrame,
    OutputTransportMessageFrame,
    OutputTransportMessageUrgentFrame,
)
from pipecat.serializers.base_serializer import FrameSerializer
from pydantic import ValidationError


class RTVIJSONSerializer(FrameSerializer):
    """Text chat only: keep eval controls and media out of the public endpoint."""

    def __init__(self):
        super().__init__(params=FrameSerializer.InputParams(ignore_rtvi_messages=False))

    async def serialize(self, frame):
        if isinstance(frame, (OutputTransportMessageFrame, OutputTransportMessageUrgentFrame)):
            return json.dumps(frame.message)
        return None

    async def deserialize(self, data):
        message_id = ""
        try:
            if not isinstance(data, str):
                raise ValueError("Send JSON text messages; binary input is not supported")
            raw = json.loads(data)
            if isinstance(raw, dict) and isinstance(raw.get("id"), str):
                message_id = raw["id"]
            message = RTVI.Message.model_validate(raw)
            if message.type not in ("client-ready", "send-text", "disconnect-bot"):
                raise ValueError(f"Unsupported message type: {message.type}")
            if message.type == "send-text":
                text = RTVI.SendTextData.model_validate(message.data)
                if not text.content.strip() or len(text.content) > 16000:
                    raise ValueError("Text must contain 1 to 16000 characters")
                options = RTVI.SendTextOptions.model_validate(
                    {
                        "audio_response": False,
                        **(text.options.model_dump(exclude_unset=True) if text.options else {}),
                    }
                )
                if options.audio_response:
                    raise ValueError("This API is text-only; set audio_response to false")
                message.data = {"content": text.content, "options": options.model_dump()}
            return InputTransportMessageFrame(message=message.model_dump())
        except (ValueError, ValidationError, RecursionError) as exc:
            return OutputTransportMessageUrgentFrame(
                message={
                    "label": RTVI.MESSAGE_LABEL,
                    "type": "error-response",
                    "id": message_id,
                    "data": {"error": str(exc)},
                }
            )
