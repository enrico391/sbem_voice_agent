"""Native Pipecat tools; identity stays outside the model's arguments."""

import asyncio
import sqlite3

from pipecat.adapters.schemas.direct_function import tool_options
from pipecat.services.llm_service import FunctionCallParams


async def _invoke(params, operation, *args):
    try:
        result = await asyncio.to_thread(getattr(params.app_resources.memory, operation), *args)
    except ValueError as exc:
        result = {"status": "error", "reason": str(exc)}
    except (sqlite3.Error, OSError):
        result = {"status": "error", "reason": "Memory storage unavailable; do not assume success"}
    await params.result_callback(result)


async def retrieve_memory(
    params: FunctionCallParams, query: str = "", limit: int = 20, offset: int = 0
):
    """Retrieve saved memories for the current user, with IDs for editing or removal.

    Args:
        query: Literal text to find, ignoring case. Empty lists all memories.
        limit: Page size from 1 to 50; defaults to 20.
        offset: Start at zero; use next_offset from a previous result for more.
    """
    await _invoke(params, "retrieve", query, limit, offset)


@tool_options(cancel_on_interruption=False)
async def remember_memory(params: FunctionCallParams, content: str):
    """Save a durable fact or preference the user asks you to remember.

    Args:
        content: One concise factual memory, from 1 to 2000 characters.
    """
    await _invoke(params, "remember", content)


@tool_options(cancel_on_interruption=False)
async def update_memory(params: FunctionCallParams, memory_id: int, content: str):
    """Replace one saved memory after the user corrects it. Retrieve its ID first.

    Args:
        memory_id: Exact ID returned by retrieve_memory or remember_memory.
        content: Corrected fact or preference, from 1 to 2000 characters.
    """
    await _invoke(params, "update", memory_id, content)


@tool_options(cancel_on_interruption=False)
async def remove_memory(params: FunctionCallParams, memory_id: int):
    """Remove one saved memory when the user asks to forget it. Retrieve its ID first.

    Args:
        memory_id: Exact ID returned by retrieve_memory or remember_memory.
    """
    await _invoke(params, "remove", memory_id)


memory_functions = [retrieve_memory, remember_memory, update_memory, remove_memory]
