"""Send a text request and stream RTVI replies until Ctrl+C or server disconnect."""

import argparse
import asyncio
import json
import os
import sys
import uuid
from pathlib import Path

from dotenv import load_dotenv
from websockets.asyncio.client import connect


async def chat(url: str, message: str):
    token = os.getenv("API_TOKEN", "").strip()
    headers = {"Authorization": f"Bearer {token}"} if token else None
    async with connect(url, additional_headers=headers) as websocket:
        await websocket.send(
            json.dumps(
                {
                    "label": "rtvi-ai",
                    "type": "client-ready",
                    "id": "ready",
                    "data": {"version": "2.2.0", "about": {"library": "sbem-example"}},
                }
            )
        )
        async with asyncio.timeout(15):
            while True:
                event = json.loads(await websocket.recv())
                if event["type"] in ("error", "error-response"):
                    raise RuntimeError(event["data"])
                if event["type"] == "bot-ready":
                    break
        await websocket.send(
            json.dumps(
                {
                    "label": "rtvi-ai",
                    "type": "send-text",
                    "id": str(uuid.uuid4()),
                    "data": {"content": message, "options": {"audio_response": False}},
                }
            )
        )
        # Keep the socket open: a tool can deliver progress and trigger later replies.
        async for raw in websocket:
            event = json.loads(raw)
            if event["type"] == "llm-raw-text":
                print(event["data"]["text"], end="", flush=True)
            elif event["type"] == "bot-llm-stopped":
                print(flush=True)
            elif event["type"] in ("error", "error-response"):
                print(event["data"], file=sys.stderr)
            elif event["type"].startswith("llm-function-call-"):
                print(f"[{event['type']}] {event['data']}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("message", help="Text to send to SBEM")
    parser.add_argument("--url", default="ws://127.0.0.1:8765/ws")
    args = parser.parse_args()
    load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)
    try:
        asyncio.run(chat(args.url, args.message))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
