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

# -------------- HELPER FUNCTIONS ----------------
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
                priority TEXT DEFAULT 'normal'
                created_at TIMESTAMP DEFAULT NOW()
            )
        """))

        conn.commit()

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

def complete_task(chat_id, task_text):
    """
    Marks a task as 'done' by matching on its text (case-insensitive,
    partial match) within this chat's open tasks. Returns True if
    something was updated, False if no match was found.
    """
    with engine.connect() as conn:
        result = conn.execute(text("""
            UPDATE tasks
            SET status = 'done'
            WHERE chat_id = :chat_id 
              AND status = 'open'
              AND task ILIKE :task_pattern
        """), {"chat_id": chat_id, "task_pattern": f"%{task_text}%"})
        conn.commit()
        return result.rowcount > 0
    
def get_completed_this_week(chat_id):
    """
    Returns tasks marked done in the last 7 days.
    """
    with engine.connect() as conn:
        result = conn.execute(text("""
            SELECT task FROM tasks
            WHERE chat_id = :chat_id 
              AND status = 'done'
              AND created_at >= NOW() - INTERVAL '7 days'
        """), {"chat_id": chat_id})
        return [row[0] for row in result.fetchall()]

def get_neglected_tasks(chat_id):
    """
    Returns tasks that are still open and were created more than
    7 days ago — these are the ones that have been sitting untouched.
    """
    with engine.connect() as conn:
        result = conn.execute(text("""
            SELECT task, created_at FROM tasks
            WHERE chat_id = :chat_id 
              AND status = 'open'
              AND created_at <= NOW() - INTERVAL '7 days'
            ORDER BY created_at ASC
        """), {"chat_id": chat_id})
        return result.fetchall()
    
def send_weekly_review():
    """
    Runs every Sunday. Summarizes what got done this week, flags
    tasks that have been sitting open too long, and asks Claude
    to generate priorities for the upcoming week.
    """
    chat_id = os.getenv("MY_CHAT_ID")

    completed = get_completed_this_week(chat_id)
    neglected = get_neglected_tasks(chat_id)
    still_open = get_open_tasks(chat_id)  # includes due_date now

    # Build readable text blocks for each category to hand to Claude
    completed_text = "\n".join([f"- {t}" for t in completed]) or "Nothing marked done this week."
    
    neglected_text = "\n".join([f"- {t[0]} (added {t[1].strftime('%b %d')})" for t in neglected]) or "Nothing neglected — nice."

    open_lines = []
    for t in still_open:
        task_id, task_text, due_date = t
        due_str = f" (due {due_date})" if due_date else ""
        open_lines.append(f"- {task_text}{due_str}")
    open_text = "\n".join(open_lines) or "No open tasks."

    prompt = f"""Here's my week:

                COMPLETED THIS WEEK:
                {completed_text}

                NEGLECTED (open more than 7 days):
                {neglected_text}

                CURRENTLY OPEN:
                {open_text}

                Give me a short weekly review:
                1. A one-line acknowledgment of what got done
                2. Call out anything neglected that I should either commit to or drop
                3. Suggest my top 3 priorities for next week

                Keep it concise and direct, this is a Sunday review not an essay."""

    response = claude_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=600,
        system="You are a personal executive assistant doing a weekly review. Be honest and concise, not falsely encouraging.",
        messages=[{"role": "user", "content": prompt}]
    )

    review_text = response.content[0].text
    send_telegram_message(chat_id, f"📋 Weekly Review\n\n{review_text}")

def send_telegram_message(chat_id: str, text: str):
    """
    Sends a text message back to a specific Telegram chat.
    chat_id tells Telegram WHICH conversation to deliver it to.
    """
    requests.post(f"{TELEGRAM_URL}/sendMessage", json={
        "chat_id": chat_id,
        "text": text
    })

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
    
def update_priority(chat_id, task_text, priority):
    """
    Updates the priority of a task matching the given text.
    priority should be 'low', 'normal', or 'high'.
    """
    with engine.connect() as conn:
        result = conn.execute(text("""
            UPDATE tasks
            SET priority = :priority
            WHERE chat_id = :chat_id 
              AND status = 'open'
              AND task ILIKE :task_pattern
        """), {"chat_id": chat_id, "priority": priority, "task_pattern": f"%{task_text}%"})
        conn.commit()
        return result.rowcount > 0
# --- DATABASE SETUP ---

# Pull the database connection string from environment variables
# (set in .env locally, and in Railway's Variables tab in production)
DATABASE_URL = os.getenv("DATABASE_URL")

# Create a SQLAlchemy "engine" — this manages the pool of connections
# to Postgres. We reuse this single engine everywhere instead of
# opening a new connection for every query.
engine = create_engine(DATABASE_URL)

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

# Schedule the job to run every day at 8:00 AM
scheduler.add_job(
    send_daily_briefing,
    CronTrigger(hour=8, minute=0, timezone=eastern),
    id="daily_briefing"
)

scheduler.add_job(
    send_weekly_review,
    CronTrigger(day_of_week="sun", hour=18, minute=0, timezone=eastern),  # Sunday 6pm Eastern
    id="weekly_review"
)

# Start the scheduler when the app boots up
scheduler.start()

# --- TELEGRAM SETUP ---

# Grab the bot token from environment variables
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# Build the base URL for all Telegram API calls using that token
TELEGRAM_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

# --- CLAUDE SETUP ---

# Defining tools for Claude to use
tools = [
    {
        "name": "create_task",
        "description": "Adds a new task to the user's to-do list. Use this whenever the user mentions something they need to do, even if not phrased as a direct command.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "The task description"
                },
                "due_date": {
                    "type": "string",
                    "description": "Due date in YYYY-MM-DD format, if mentioned. Omit if no due date given."
                }
            },
            "required": ["task"]
        }
    },
    {
        "name": "complete_task",
        "description": "Marks an existing task as done. Use this when the user indicates they finished or completed something.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "Text describing which task to mark complete — doesn't need to be exact, partial match is fine"
                }
            },
            "required": ["task"]
        }
    },
    {
        "name": "update_priority",
        "description": "Changes the priority level of an existing task. Use this when the user indicates something is more or less important/urgent.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "Text describing which task to update"
                },
                "priority": {
                    "type": "string",
                    "enum": ["low", "normal", "high"],
                    "description": "The new priority level"
                }
            },
            "required": ["task", "priority"]
        }
    }
]

# Create the Anthropic client using our API key — this object is what
# we call .messages.create() on whenever we want a response from Claude
claude_client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

@app.get("/")
def health_check():
    """
    Simple endpoint to confirm the app is alive.
    Visiting the base URL in a browser hits this — useful for quick
    sanity checks without needing Telegram involved at all.
    """
    return {"status": "alive"}

@app.post("/webhook")
async def webhook(request: Request):
    """
    Main entry point. Every message goes to Claude along with the
    available tools. Claude decides whether to call a function
    (create_task, complete_task, update_priority) or just respond
    conversationally.
    """
    data = await request.json()
    message = data.get("message", {})
    chat_id = str(message.get("chat", {}).get("id"))
    user_text = message.get("text", "")

    if not user_text or not chat_id:
        return {"ok": True}

    save_message(chat_id, "user", user_text)
    history = get_conversation_history(chat_id)

    # First call to Claude — pass the tools so it can choose to use them
    response = claude_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system="You are a personal executive assistant. Use the available tools when the user wants to manage tasks. Otherwise just respond conversationally.",
        messages=history,
        tools=tools
    )

    # Claude's response can contain text, a tool call, or both.
    # We need to check what type of content block(s) came back.
    tool_results = []
    final_reply_text = ""

    for block in response.content:
        if block.type == "text":
            final_reply_text += block.text

        elif block.type == "tool_use":
            tool_name = block.name
            tool_input = block.input

            # Run the actual Python function that matches what Claude requested
            if tool_name == "create_task":
                add_task(chat_id, tool_input["task"], tool_input.get("due_date"))
                result_text = f"Task added: {tool_input['task']}"

            elif tool_name == "complete_task":
                success = complete_task(chat_id, tool_input["task"])
                result_text = "Marked as done" if success else "Couldn't find that task"

            elif tool_name == "update_priority":
                success = update_priority(chat_id, tool_input["task"], tool_input["priority"])
                result_text = f"Priority updated to {tool_input['priority']}" if success else "Couldn't find that task"

            else:
                result_text = "Unknown tool"

            # Claude needs to know what happened so it can respond naturally
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": result_text
            })

    # If Claude called a tool, we need a SECOND call — sending back
    # the tool result so Claude can phrase a natural reply about it
    if tool_results:
        follow_up = claude_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system="You are a personal executive assistant. Use the available tools when the user wants to manage tasks. Otherwise just respond conversationally.",
            messages=history + [
                {"role": "assistant", "content": response.content},
                {"role": "user", "content": tool_results}
            ],
            tools=tools
        )
        final_reply_text = follow_up.content[0].text

    save_message(chat_id, "assistant", final_reply_text)
    send_telegram_message(chat_id, final_reply_text)

    return {"ok": True}