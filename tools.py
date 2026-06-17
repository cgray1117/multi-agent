"""
tools.py

Claude only accepts ONE flat tools=[...] list per API call — it has
no concept of "agents." This file combines every agent's tool
definitions into a single list, and provides one dispatcher function
that tries each agent in turn until one of them recognizes the tool.
"""

from agents.chief_of_staff import chief_of_staff_tools, handle_tool_call as cos_handle
from agents.career import career_tools, handle_tool_call as career_handle
from agents.wellness import wellness_tools, handle_tool_call as wellness_handle

# The single combined list Claude actually sees
all_tools = chief_of_staff_tools + career_tools + wellness_tools


def dispatch_tool_call(tool_name, tool_input, chat_id):
    """
    Tries each agent's handler in turn. Each agent's handle_tool_call
    returns None if the tool_name isn't one of theirs, so we just
    fall through to the next agent until one returns a real result.
    """
    result = cos_handle(tool_name, tool_input, chat_id)
    if result is not None:
        return result

    result = career_handle(tool_name, tool_input, chat_id)
    if result is not None:
        return result

    result = wellness_handle(tool_name, tool_input, chat_id)
    if result is not None:
        return result

    return "Unknown tool"