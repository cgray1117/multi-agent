"""
agents/wellness.py

Physical health (calisthenics, cardio, sleep, diet) and mental health
(therapy, journaling, meditation) tracking. Built on the shared
habits / habit_logs tables defined in database.py.

Follows the exact same pattern as agents/chief_of_staff.py:
functions, a tool list, and a handle_tool_call() dispatcher.
"""

import os
from sqlalchemy import text
from database import engine
from telegram import send_telegram_message
import anthropic

claude_client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


# --- HABIT DEFINITIONS (one-time setup) ---

# Your actual physical + mental health habits, with how often you
# intend to do each one. This only needs to run once — seed_habits()
# checks for duplicates before inserting so it's safe to call again.
WELLNESS_HABITS = [
    ("physical", "calisthenics", "3x/week"),
    ("physical", "cardio walk", "daily"),
    ("physical", "sleep 8 hours", "daily"),
    ("physical", "clean eating", "daily"),
    ("mental", "therapy", "biweekly"),
    ("mental", "journaling", "daily"),
    ("mental", "meditation", "weekly"),
]


def seed_habits(chat_id):
    """
    Inserts the WELLNESS_HABITS list into the habits table for this
    chat_id, but only for habits that don't already exist — checked
    by matching on name. Safe to call multiple times without creating
    duplicates.
    """
    with engine.connect() as conn:
        for category, name, frequency in WELLNESS_HABITS:
            existing = conn.execute(text("""
                SELECT id FROM habits
                WHERE chat_id = :chat_id AND name = :name
            """), {"chat_id": chat_id, "name": name}).fetchone()

            if not existing:
                conn.execute(text("""
                    INSERT INTO habits (chat_id, category, name, target_frequency)
                    VALUES (:chat_id, :category, :name, :frequency)
                """), {
                    "chat_id": chat_id,
                    "category": category,
                    "name": name,
                    "frequency": frequency
                })
        conn.commit()


def get_habit_by_name(chat_id, name_fragment):
    """
    Finds a habit by partial, case-insensitive name match within
    the physical/mental categories only — so logging "walk" matches
    "cardio walk" without needing the exact phrase.
    """
    with engine.connect() as conn:
        result = conn.execute(text("""
            SELECT id, name, category, target_frequency FROM habits
            WHERE chat_id = :chat_id
              AND category IN ('physical', 'mental')
              AND name ILIKE :pattern
            LIMIT 1
        """), {"chat_id": chat_id, "pattern": f"%{name_fragment}%"})
        return result.fetchone()


def log_habit(chat_id, name_fragment, note=None):
    """
    Logs an occurrence of a habit right now. Returns the habit name
    if successful, or None if no matching habit was found.
    """
    habit = get_habit_by_name(chat_id, name_fragment)
    if not habit:
        return None

    habit_id = habit[0]
    habit_name = habit[1]

    with engine.connect() as conn:
        conn.execute(text("""
            INSERT INTO habit_logs (chat_id, habit_id, note)
            VALUES (:chat_id, :habit_id, :note)
        """), {"chat_id": chat_id, "habit_id": habit_id, "note": note})
        conn.commit()

    return habit_name


def get_habit_adherence(chat_id, days=30):
    """
    For every physical/mental habit, counts how many times it was
    logged in the last `days` days. Returns a list of
    (name, category, target_frequency, log_count) tuples — the raw
    data Claude needs to reason about patterns and gaps.
    """
    with engine.connect() as conn:
        result = conn.execute(text("""
            SELECT 
                h.name,
                h.category,
                h.target_frequency,
                COUNT(l.id) AS log_count
            FROM habits h
            LEFT JOIN habit_logs l 
                ON l.habit_id = h.id 
                AND l.logged_at >= NOW() - INTERVAL :days_interval
            WHERE h.chat_id = :chat_id
              AND h.category IN ('physical', 'mental')
            GROUP BY h.name, h.category, h.target_frequency
            ORDER BY h.category, h.name
        """), {"chat_id": chat_id, "days_interval": f"{days} days"})
        return result.fetchall()


# --- ANALYSIS / SUMMARY ---

def generate_wellness_snapshot(chat_id):
    """
    Pulls 30-day adherence data for every physical/mental habit and
    asks Claude to summarize trends, flag what's slipping, and call
    out anything worth addressing. This is the function behind both
    the 'how am I trending' on-demand command and any scheduled
    wellness check-ins.
    """
    adherence = get_habit_adherence(chat_id, days=30)

    if not adherence:
        return "No wellness habits set up yet."

    lines = []
    for name, category, target, count in adherence:
        lines.append(f"- [{category}] {name}: target {target}, logged {count}x in last 30 days")
    data_text = "\n".join(lines)

    prompt = f"""Here's my physical and mental health habit data for the last 30 days:

                {data_text}

                Give me an honest snapshot:
                1. What's actually on track vs. target frequency
                2. What's slipping or being neglected
                3. One thing worth focusing on this week

                Be direct, not falsely encouraging. Keep it concise."""

    response = claude_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=500,
        system="You are a wellness-focused assistant. Be honest and specific about patterns, not vague encouragement.",
        messages=[{"role": "user", "content": prompt}]
    )

    return response.content[0].text


def send_evening_checkin():
    """
    Scheduled job — fires in the evening and asks about the day's
    physical/mental habits directly, rather than waiting for the
    user to log proactively. This is the 'bot asks' half of the
    logging approach; the Claude tool calls below are the 'I log
    anytime' half.
    """
    chat_id = os.getenv("MY_CHAT_ID")
    send_telegram_message(
        chat_id,
        "🌙 Evening check-in: Did you get your walk in today? "
        "How about journaling? Anything else physical/mental health-wise "
        "you want to log — just tell me directly."
    )


# --- CLAUDE TOOL DEFINITIONS (Wellness) ---

wellness_tools = [
    {
        "name": "log_wellness_habit",
        "description": "Logs that a physical or mental health habit happened just now or recently — e.g. calisthenics, a walk, therapy, journaling, meditation, good sleep, clean eating. Use this whenever the user mentions doing (or explicitly skipping) one of these.",
        "input_schema": {
            "type": "object",
            "properties": {
                "habit": {
                    "type": "string",
                    "description": "Which habit, in the user's own words — e.g. 'walk', 'calisthenics', 'therapy', 'meditation', 'journaling', 'sleep', 'diet'"
                },
                "note": {
                    "type": "string",
                    "description": "Optional context, e.g. 'only did 20 min' or 'skipped, low energy'. Omit if not relevant."
                }
            },
            "required": ["habit"]
        }
    },
    {
        "name": "get_wellness_snapshot",
        "description": "Analyzes the user's physical and mental health habit adherence over the last 30 days and summarizes what's on track vs. slipping. Use this when the user asks how they're doing physically/mentally, how they're trending, or similar.",
        "input_schema": {"type": "object", "properties": {}, "required": []}
    }
]


def handle_tool_call(tool_name, tool_input, chat_id):
    """
    Executes a Wellness agent tool call and returns the result text.
    Returns None if tool_name doesn't belong to this agent.
    """
    if tool_name == "log_wellness_habit":
        habit_name = log_habit(chat_id, tool_input["habit"], tool_input.get("note"))
        if habit_name:
            return f"Logged: {habit_name}"
        else:
            return f"Couldn't find a wellness habit matching '{tool_input['habit']}'"

    elif tool_name == "get_wellness_snapshot":
        return generate_wellness_snapshot(chat_id)

    return None  # not a Wellness tool