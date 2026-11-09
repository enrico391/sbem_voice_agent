"""Conversation controls that leave the always-on worker running."""

from pipecat.adapters.schemas.direct_function import tool_options
from pipecat.services.llm_service import FunctionCallParams


@tool_options(cancel_on_interruption=False)
async def end_conversation(params: FunctionCallParams):
    """Return to wake-word listening after the final reply when the user is done.

    Use for a clear goodbye or a request to stop listening. This ends the current
    conversation, not the application or any robot motion. After the tool result,
    give a brief farewell. A new wake starts a fresh conversation context.
    """
    result = await params.app_resources.session.request_end()
    await params.result_callback(result)
