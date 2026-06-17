"""
agents/career.py

Career agent — income tracking, consulting pipeline, learning goals.
Empty shell for now. Functions, career_tools list, and
handle_tool_call() will be added here following the exact same
pattern as agents/chief_of_staff.py.
"""

import os
from sqlalchemy import text
from database import engine
from telegram import send_telegram_message
import anthropic

claude_client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


# --- Functions go here: add_client, log_revenue, update_pipeline_stage, etc. ---


# --- Tool definitions go here, same pattern as chief_of_staff_tools ---
career_tools = []


def handle_tool_call(tool_name, tool_input, chat_id):
    """
    Executes a Career agent tool call and returns the result text.
    Returns None if tool_name doesn't belong to this agent.
    """
    return None  # no career tools yet