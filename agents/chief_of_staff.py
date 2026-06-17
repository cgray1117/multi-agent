"""
agents/chief_of_staff.py

Everything related to task management, daily briefings, and weekly
reviews. This is all logic you already built and tested in main.py —
it's been moved here as-is, just with imports adjusted.
"""

import os
from sqlalchemy import text
from database import engine
from telegram import send_telegram_message
import anthropic

claude_client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


# --- TASK CRUD ---

def add_task(chat_id, task_text, due_date=None):
    """Inserts a new task into the tasks table with status 'open'."""
    with engine.connect() as conn:
        conn.execute(text("""
            INSERT INTO tasks (chat_id, task, status, due_date)
            VALUES (:chat_id, :task, 'open', :due_date)
        """), {"chat_id": chat_id, "task": task_text, "due_date": due_date})
        conn.commit()


def get_open_tasks(chat_id):
    """Fetches all open tasks for this chat, including due dates."""
    with engine.connect() as conn:
        result = conn.execute(text("""
            SELECT id, task, due_date FROM tasks
            WHERE chat_id = :chat_id AND status = 'open'
            ORDER BY created_at ASC
        """), {"chat_id": chat_id})
        return result.fetchall()


def complete_task(chat_id, task_text):
    """
    Marks a task as 'done' by partial, case-insensitive match.
    Returns True if something was updated, False if no match found.
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


def update_priority(chat_id, task_text, priority):
    """Updates the priority ('low', 'normal', 'high') of a matching task."""
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


def get_completed_this_week(chat_id):
    """Returns tasks marked done in the last 7 days."""
    with engine.connect() as conn:
        result = conn.execute(text("""
            SELECT task FROM tasks
            WHERE chat_id = :chat_id 
              AND status = 'done'
              AND created_at >= NOW() - INTERVAL '7 days'
        """), {"chat_id": chat_id})
        return [row[0] for row in result.fetchall()]


def get_neglected_tasks(chat_id):
    """Returns open tasks created more than 7 days ago."""
    with engine.connect() as conn:
        result = conn.execute(text("""
            SELECT task, created_at FROM tasks
            WHERE chat_id = :chat_id 
              AND status = 'open'
              AND created_at <= NOW() - INTERVAL '7 days'
            ORDER BY created_at ASC
        """), {"chat_id": chat_id})
        return result.fetchall()


# --- PRIORITIZATION / BRIEFINGS ---

def generate_priority_summary(chat_id):
    """
    Pulls open tasks and asks Claude to rank them by urgency and
    energy cost. Used by both the scheduled morning briefing and
    the on-demand 'focus' command.
    """
    tasks = get_open_tasks(chat_id)

    if not tasks:
        return "You have no open tasks right now. 🎉"

    task_lines = []
    for t in tasks:
        task_id, task_text, due_date = t
        due_str = f" (due {due_date})" if due_date else ""
        task_lines.append(f"- {task_text}{due_str}")
    task_list_text = "\n".join(task_lines)

    prompt = f"""Here are my open tasks:

{task_list_text}

Rank these by urgency (factoring in due dates if present) and 
estimated energy cost (how much focus/effort each likely takes).
Return the top 3 I should focus on right now, with a one-sentence 
reason for each. Keep it concise."""

    response = claude_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=500,
        system="You are a personal executive assistant helping prioritize. Be direct and practical.",
        messages=[{"role": "user", "content": prompt}]
    )

    return response.content[0].text


def send_daily_briefing():
    """Scheduled job (8am Eastern) — sends the morning priority briefing."""
    chat_id = os.getenv("MY_CHAT_ID")
    summary = generate_priority_summary(chat_id)
    send_telegram_message(chat_id, f"🌅 Good morning! Here's your focus for today:\n\n{summary}")


def send_weekly_review():
    """
    Scheduled job (Sundays) — summarizes what got done this week,
    flags neglected tasks, and generates next week's priorities.
    """
    chat_id = os.getenv("MY_CHAT_ID")

    completed = get_completed_this_week(chat_id)
    neglected = get_neglected_tasks(chat_id)
    still_open = get_open_tasks(chat_id)

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


# --- CLAUDE TOOL DEFINITIONS (Chief of Staff) ---

# These get combined with other agents' tools in tools.py before
# being passed to Claude. Keeping them here means the tool
# definition lives next to the function it actually calls.
chief_of_staff_tools = [
    {
        "name": "create_task",
        "description": "Adds a new task to the user's to-do list. Use this whenever the user mentions something they need to do, even if not phrased as a direct command.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "The task description"},
                "due_date": {"type": "string", "description": "Due date in YYYY-MM-DD format, if mentioned. Omit if no due date given."}
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
                "task": {"type": "string", "description": "Text describing which task to mark complete — partial match is fine"}
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
                "task": {"type": "string", "description": "Text describing which task to update"},
                "priority": {"type": "string", "enum": ["low", "normal", "high"], "description": "The new priority level"}
            },
            "required": ["task", "priority"]
        }
    },
    {
        "name": "list_tasks",
        "description": "Retrieves the user's current open tasks. Use this whenever the user asks what they need to do, what's on their list, or anything similar.",
        "input_schema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "get_focus_priorities",
        "description": "Analyzes the user's open tasks and returns the top priorities to focus on right now, ranked by urgency and effort. Use this when the user asks what to focus on, what's most important, or what they should work on.",
        "input_schema": {"type": "object", "properties": {}, "required": []}
    }
]


def handle_tool_call(tool_name, tool_input, chat_id):
    """
    Executes a Chief of Staff tool call and returns the result text.
    Returns None if tool_name doesn't belong to this agent — lets
    the main dispatcher in main.py know to check other agents.
    """
    if tool_name == "create_task":
        add_task(chat_id, tool_input["task"], tool_input.get("due_date"))
        return f"Task added: {tool_input['task']}"

    elif tool_name == "complete_task":
        success = complete_task(chat_id, tool_input["task"])
        return "Marked as done" if success else "Couldn't find that task"

    elif tool_name == "update_priority":
        success = update_priority(chat_id, tool_input["task"], tool_input["priority"])
        return f"Priority updated to {tool_input['priority']}" if success else "Couldn't find that task"

    elif tool_name == "list_tasks":
        tasks = get_open_tasks(chat_id)
        if not tasks:
            return "No open tasks."
        task_lines = []
        for t in tasks:
            task_id, task_text, due_date = t
            due_str = f" (due {due_date})" if due_date else ""
            task_lines.append(f"{task_text}{due_str}")
        return "; ".join(task_lines)

    elif tool_name == "get_focus_priorities":
        return generate_priority_summary(chat_id)

    return None  # not a Chief of Staff tool