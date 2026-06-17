from fastapi import FastAPI, Request
from dotenv import load_dotenv
import os
import json
import requests
import anthropic
import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

from sqlalchemy import create_engine, text

# --- DATABASE SETUP ---

# Pull the database connection string from environment variables
# (set in .env locally, and in Railway's Variables tab in production)
DATABASE_URL = os.getenv("DATABASE_URL")

# Create a SQLAlchemy "engine" — this manages the pool of connections
# to Postgres. We reuse this single engine everywhere instead of
# opening a new connection for every query.
engine = create_engine(DATABASE_URL)


def create_tables():
    """
    Creates the database tables if they don't already exist.
    Safe to run every time the app starts — IF NOT EXISTS means
    it won't wipe or duplicate tables on restart/redeploy.
    """
    with engine.connect() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS messages (
                id SERIAL PRIMARY KEY,
                chat_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """))

        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS tasks (
                id SERIAL PRIMARY KEY,
                chat_id TEXT NOT NULL,
                task TEXT NOT NULL,
                status TEXT DEFAULT 'open',
                due_date DATE,               -- NEW: optional due date, can be NULL
                created_at TIMESTAMP DEFAULT NOW()
            )
        """))

        conn.commit()


# Run table creation once when the app starts up.
# This means every fresh deploy/restart guarantees the tables exist.
create_tables()

# Set up basic logging so we can print useful debug info to Railway's logs
logger = logging.getLogger(__name__)

# load_dotenv() is commented out because Railway injects env vars directly —
# this is only needed when running locally to read from a .env file
# load_dotenv()

# Create the FastAPI app instance — this is the core of our web server
app = FastAPI()

# --- SCHEDULER SETUP ---

# BackgroundScheduler runs jobs in a separate thread so they don't
# block your FastAPI app from handling normal webhook requests
scheduler = BackgroundScheduler()

# Define your timezone once — Railway's server runs on UTC by default,
# so this tells APScheduler to convert "8am" into whatever UTC time
# actually corresponds to 8am Eastern, including automatic handling
# of daylight saving time shifts (EDT vs EST)
eastern = pytz.timezone("America/New_York")


import json

def send_daily_briefing():
    """
    Pulls all open tasks for the user, sends them to Claude to be
    ranked by urgency and estimated energy cost, then sends the
    top priorities back as a morning briefing via Telegram.
    """
    chat_id = os.getenv("MY_CHAT_ID")
    tasks = get_open_tasks(chat_id)

    # If there's nothing to rank, just say so and stop early
    if not tasks:
        send_telegram_message(chat_id, "🌅 Good morning! You have no open tasks today.")
        return

    # Build a plain-text list of tasks (with due dates if present)
    # to hand to Claude as context
    task_lines = []
    for t in tasks:
        _, task_text, due_date = t
        due_str = f" (due {due_date})" if due_date else ""
        task_lines.append(f"- {task_text}{due_str}")
    task_list_text = "\n".join(task_lines)

    # Ask Claude to rank them. We're explicit about the output format
    # so we can reliably parse it back out afterward.
    prompt = f"""Here are my open tasks:

                {task_list_text}

                Rank these by urgency (factoring in due dates if present) and 
                estimated energy cost (how much focus/effort each likely takes).
                Return the top 3 I should focus on today, with a one-sentence 
                reason for each. Keep it concise — this is a morning briefing,
                not a report."""

    response = claude_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=500,
        system="You are a personal executive assistant helping prioritize a busy day. Be direct and practical.",
        messages=[{"role": "user", "content": prompt}]
    )

    briefing_text = response.content[0].text

    # Send the AI-generated briefing back to Telegram
    send_telegram_message(chat_id, f"🌅 Good morning! Here's your focus for today:\n\n{briefing_text}")

# Schedule the job to run every day at 8:00 AM
scheduler.add_job(
    send_daily_briefing,
    CronTrigger(hour=8, minute=0, timezone=eastern),
    id="daily_briefing"
)

# Start the scheduler when the app boots up
scheduler.start()

# --- TELEGRAM SETUP ---

# Grab the bot token from environment variables
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# Build the base URL for all Telegram API calls using that token
TELEGRAM_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

# --- CLAUDE SETUP ---

# Create the Anthropic client using our API key — this object is what
# we call .messages.create() on whenever we want a response from Claude
claude_client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


def send_telegram_message(chat_id: str, text: str):
    """
    Sends a text message back to a specific Telegram chat.
    chat_id tells Telegram WHICH conversation to deliver it to.
    """
    requests.post(f"{TELEGRAM_URL}/sendMessage", json={
        "chat_id": chat_id,
        "text": text
    })


@app.get("/")
def health_check():
    """
    Simple endpoint to confirm the app is alive.
    Visiting the base URL in a browser hits this — useful for quick
    sanity checks without needing Telegram involved at all.
    """
    return {"status": "alive"}


def get_conversation_history(chat_id):
    """
    Retrieves the last 10 messages for this chat so we can give
    Claude conversational context instead of treating every message
    as a brand new, isolated question.
    """
    with engine.connect() as conn:
        result = conn.execute(text("""
            SELECT role, content FROM messages
            WHERE chat_id = :chat_id
            ORDER BY created_at DESC
            LIMIT 10
        """), {"chat_id": chat_id})

        messages = result.fetchall()

        # Messages come back newest-first from the query, but Claude
        # needs them oldest-first (chronological order), so we reverse them.
        # We also convert each database row into the dict format
        # Claude's API expects: {"role": ..., "content": ...}
        return [{"role": row[0], "content": row[1]} for row in reversed(messages)]


def save_message(chat_id, role, content):
    """
    Saves a single message (either from the user or from Claude)
    into the messages table, tagged with which chat it belongs to.
    """
    with engine.connect() as conn:
        conn.execute(text("""
            INSERT INTO messages (chat_id, role, content)
            VALUES (:chat_id, :role, :content)
        """), {"chat_id": chat_id, "role": role, "content": content})
        conn.commit()


def add_task(chat_id, task_text, due_date=None):
    """
    Inserts a new task into the tasks table with status 'open'.
    due_date is optional — pass None if no due date is given.
    """
    with engine.connect() as conn:
        conn.execute(text("""
            INSERT INTO tasks (chat_id, task, status, due_date)
            VALUES (:chat_id, :task, 'open', :due_date)
        """), {"chat_id": chat_id, "task": task_text, "due_date": due_date})
        conn.commit()


def get_open_tasks(chat_id):
    """
    Fetches all open tasks for this chat, including due dates,
    ordered from oldest to newest.
    """
    with engine.connect() as conn:
        result = conn.execute(text("""
            SELECT id, task, due_date FROM tasks
            WHERE chat_id = :chat_id AND status = 'open'
            ORDER BY created_at ASC
        """), {"chat_id": chat_id})
        return result.fetchall()


@app.post("/webhook")
async def webhook(request: Request):
    """
    Main entry point — Telegram sends every incoming message here
    as a POST request. This function decides what to do with it:
    add a task, list tasks, or fall through to Claude for a normal reply.
    """
    # Parse the incoming JSON body from Telegram
    data = await request.json()

    # Telegram nests everything inside a "message" object.
    # We use .get() with defaults so this doesn't crash if the
    # update is something other than a normal text message
    # (e.g. a sticker, a join event, etc.)
    message = data.get("message", {})
    chat_id = str(message.get("chat", {}).get("id"))
    user_text = message.get("text", "")

    # If there's no text or no chat_id, there's nothing useful to do —
    # acknowledge receipt and exit early.
    if not user_text or not chat_id:
        return {"ok": True}

    # --- COMMAND: "add task ___" ---
    if user_text.lower().startswith("add task"):
        # Strip off the literal "add task" prefix (8 characters),
        # then remove any leading colon and extra whitespace so
        # both "add task: clean car" and "add task clean car" work
        task_text = user_text[8:].lstrip(":").strip()

        if task_text:
            add_task(chat_id, task_text)
            send_telegram_message(chat_id, f"Added task: {task_text}")
        else:
            # User typed "add task" with nothing after it
            send_telegram_message(chat_id, "What's the task? Try: add task call the venue")

        # Stop here — this command doesn't need to go to Claude at all
        return {"ok": True}

    # --- COMMAND: "my tasks" ---
    if user_text.lower().strip() == "my tasks":
        tasks = get_open_tasks(chat_id)

        if not tasks:
            send_telegram_message(chat_id, "You have no open tasks. 🎉")
        else:
            # Build a numbered list like "1. task one\n2. task two"
            # t[1] is the task text column (t[0] would be the id)
            task_list = "\n".join([f"{i+1}. {t[1]}" for i, t in enumerate(tasks)])
            send_telegram_message(chat_id, f"Your open tasks:\n{task_list}")

        return {"ok": True}

    # --- DEFAULT: send to Claude for a normal conversational reply ---

    # Save the user's message first so it's included in their own history
    save_message(chat_id, "user", user_text)

    # Pull the last 10 messages (including the one we just saved)
    # to give Claude conversational context
    history = get_conversation_history(chat_id)

    # Call Claude with the system prompt (defines its role/personality)
    # and the full conversation history
    response = claude_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system="You are a personal executive assistant. Be concise and practical.",
        messages=history
    )

    # Claude's reply text lives inside the first content block
    reply = response.content[0].text

    # Save Claude's reply too, so future messages have it as context
    save_message(chat_id, "assistant", reply)

    # Send the reply back to the user in Telegram
    send_telegram_message(chat_id, reply)

    return {"ok": True}