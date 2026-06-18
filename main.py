"""
main.py

The FastAPI app itself. This file is intentionally thin — it owns
the webhook route and the scheduler, and delegates all actual logic
to database.py, telegram.py, tools.py, and the agents/ folder.
"""

import os
import anthropic
from fastapi import FastAPI, Request
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

from database import engine, create_tables
from telegram import send_telegram_message
from tools import all_tools, dispatch_tool_call
from agents.chief_of_staff import send_daily_briefing, send_weekly_review
from agents.wellness import seed_habits, seed_exercises, send_evening_checkin, send_weekly_workout_summary


# --- APP + DB SETUP ---

app = FastAPI()
create_tables()  # safe to run every startup — IF NOT EXISTS guards it

# Seed your wellness habits once — seed_habits() checks for existing
# entries before inserting, so this is safe to call on every startup
seed_habits(os.getenv("MY_CHAT_ID"))
seed_exercises()  # no chat_id needed — exercises are global, not per-user


claude_client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


# --- CONVERSATION MEMORY (shared across all agents) ---

from sqlalchemy import text

def get_conversation_history(chat_id):
    """Retrieves the last 10 messages for this chat for Claude context."""
    with engine.connect() as conn:
        result = conn.execute(text("""
            SELECT role, content FROM messages
            WHERE chat_id = :chat_id
            ORDER BY created_at DESC
            LIMIT 10
        """), {"chat_id": chat_id})
        messages = result.fetchall()
        return [{"role": row[0], "content": row[1]} for row in reversed(messages)]


def save_message(chat_id, role, content):
    """Saves a message into the messages table."""
    with engine.connect() as conn:
        conn.execute(text("""
            INSERT INTO messages (chat_id, role, content)
            VALUES (:chat_id, :role, :content)
        """), {"chat_id": chat_id, "role": role, "content": content})
        conn.commit()


# --- SCHEDULER SETUP ---

scheduler = BackgroundScheduler()
eastern = pytz.timezone("America/New_York")

scheduler.add_job(
    send_daily_briefing,
    CronTrigger(hour=8, minute=0, timezone=eastern),
    id="daily_briefing"
)

scheduler.add_job(
    send_weekly_review,
    CronTrigger(day_of_week="sun", hour=18, minute=0, timezone=eastern),
    id="weekly_review"
)

scheduler.add_job(
    send_evening_checkin,
    CronTrigger(hour=21, minute=0, timezone=eastern),  # 9pm Eastern
    id="evening_checkin"
)

scheduler.add_job(
    send_weekly_workout_summary,
    CronTrigger(day_of_week="mon", hour=8, minute=15, timezone=eastern),
    id="weekly_workout_summary"
)

scheduler.start()


# --- ROUTES ---

@app.get("/")
def health_check():
    """Confirms the app is alive — visit the base URL to check."""
    return {"status": "alive"}


@app.post("/webhook")
async def webhook(request: Request):
    """
    Main entry point. Every Telegram message lands here. We send it
    to Claude along with the FULL combined tool list from every
    agent — Claude decides which tool (if any) to call, and
    dispatch_tool_call() routes the execution to whichever agent
    actually owns that tool.
    """
    data = await request.json()
    message = data.get("message", {})
    chat_id = str(message.get("chat", {}).get("id"))
    user_text = message.get("text", "")

    if not user_text or not chat_id:
        return {"ok": True}

    save_message(chat_id, "user", user_text)
    history = get_conversation_history(chat_id)

    response = claude_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system="You are a personal multiagent assistant covering executive/task management, career tracking, and wellness habit tracking (physical and mental health). Use the available tools when relevant. Otherwise respond conversationally.",
        messages=history,
        tools=all_tools
    )

    tool_results = []
    final_reply_text = ""

    for block in response.content:
        if block.type == "text":
            final_reply_text += block.text

        elif block.type == "tool_use":
            result_text = dispatch_tool_call(block.name, block.input, chat_id)
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": result_text
            })

    if tool_results:
        follow_up = claude_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system="You are a personal multiagent assistant covering executive/task management, career tracking, and wellness habit tracking (physical and mental health). Use the available tools when relevant. Otherwise respond conversationally.",
            messages=history + [
                {"role": "assistant", "content": response.content},
                {"role": "user", "content": tool_results}
            ],
            tools=all_tools
        )
        final_reply_text = follow_up.content[0].text

    save_message(chat_id, "assistant", final_reply_text)
    send_telegram_message(chat_id, final_reply_text)

    return {"ok": True}