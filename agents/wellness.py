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

# --- WORKOUT LOGGER ---

# Your calisthenics exercise library — seeded once into the exercises
# table. These are the definitions; workouts table stores the actual
# logged sessions that reference these by id.
CALISTHENICS_EXERCISES = [
    ("pushups", "chest, triceps, anterior deltoid", "core"),
    ("pull-ups", "latissimus dorsi, biceps", "rear deltoid, core"),
    ("inverted rows", "latissimus dorsi, biceps, rear deltoid", "core, forearms"),
    ("dips", "triceps, chest", "anterior deltoid"),
    ("squats", "quadriceps, glutes", "hamstrings, core"),
    ("lunges", "quadriceps, glutes", "hamstrings, calves"),
    ("plank", "core, transverse abdominis", "shoulders, glutes"),
    ("burpees", "full body, cardio", "core, shoulders"),
    ("mountain climbers", "core, cardio", "shoulders, hip flexors"),
    ("jump squats", "quadriceps, glutes, cardio", "calves, core"),
    ("pike pushups", "shoulders, triceps", "core"),
    ("glute bridges", "glutes, hamstrings", "core, lower back"),
    ("calf raises", "calves", "ankles"),
]


def seed_exercises():
    """
    Inserts CALISTHENICS_EXERCISES into the exercises table, skipping
    any that already exist by name. Safe to call on every startup.
    """
    with engine.connect() as conn:
        for name, primary, secondary in CALISTHENICS_EXERCISES:
            existing = conn.execute(text("""
                SELECT id FROM exercises WHERE name = :name
            """), {"name": name}).fetchone()

            if not existing:
                conn.execute(text("""
                    INSERT INTO exercises (name, primary_muscles_worked, secondary_muscles_worked)
                    VALUES (:name, :primary, :secondary)
                """), {"name": name, "primary": primary, "secondary": secondary})
        conn.commit()


def get_or_create_exercise(name_fragment):
    """
    Finds an exercise by partial, case-insensitive name match.
    If no match is found, creates a new exercise entry with just
    the name so novel exercises (e.g. a new movement you try) can
    still be logged without crashing.
    Returns the exercise id and canonical name.
    """
    with engine.connect() as conn:
        # Try to find existing match first
        result = conn.execute(text("""
            SELECT id, name FROM exercises
            WHERE name ILIKE :pattern
            LIMIT 1
        """), {"pattern": f"%{name_fragment}%"}).fetchone()

        if result:
            return result[0], result[1]

        # Create a new exercise entry if nothing matched
        new_exercise = conn.execute(text("""
            INSERT INTO exercises (name)
            VALUES (:name)
            RETURNING id, name
        """), {"name": name_fragment}).fetchone()
        conn.commit()
        return new_exercise[0], new_exercise[1]


def log_workout(chat_id, exercise_name, sets=None, reps_per_set=None,
                duration_minutes=None, note=None):
    """
    Logs a workout session. Finds or creates the exercise first,
    then writes a row to workouts with whatever detail was provided.
    All fields except exercise_name are optional — you might log
    "did a 20 min walk" (duration only) or "3 sets of 10 pushups"
    (sets + reps, no duration).
    Returns the canonical exercise name so Claude can confirm it.
    """
    exercise_id, canonical_name = get_or_create_exercise(exercise_name)

    with engine.connect() as conn:
        conn.execute(text("""
            INSERT INTO workouts
                (chat_id, exercise_id, sets, reps_per_set, workout_duration_minutes, note)
            VALUES
                (:chat_id, :exercise_id, :sets, :reps_per_set, :duration, :note)
        """), {
            "chat_id": chat_id,
            "exercise_id": exercise_id,
            "sets": sets,
            "reps_per_set": reps_per_set,
            "duration": duration_minutes,
            "note": note
        })
        conn.commit()

    return canonical_name


def get_workout_history(chat_id, days=30):
    """
    Returns all workout logs for the last `days` days, joined with
    exercise names so the output is human-readable. Claude uses this
    to summarize workout patterns and frequency.
    """
    with engine.connect() as conn:
        result = conn.execute(text("""
            SELECT
                e.name,
                w.sets,
                w.reps_per_set,
                w.workout_duration_minutes,
                w.note,
                w.logged_at
            FROM workouts w
            JOIN exercises e ON e.id = w.exercise_id
            WHERE w.chat_id = :chat_id
              AND w.logged_at >= NOW() - INTERVAL :days_interval
            ORDER BY w.logged_at DESC
        """), {"chat_id": chat_id, "days_interval": f"{days} days"})
        return result.fetchall()


def get_workout_summary(chat_id):
    """
    Pulls the last 30 days of workout history and asks Claude to
    summarize frequency, variety, and any patterns worth noting.
    This is the 'how's my training looking' on-demand command.
    """
    history = get_workout_history(chat_id, days=30)

    if not history:
        return "No workouts logged in the last 30 days."

    lines = []
    for name, sets, reps, duration, note, logged_at in history:
        date_str = logged_at.strftime("%b %d")
        detail_parts = []
        if sets and reps:
            detail_parts.append(f"{sets} sets x {reps} reps")
        if duration:
            detail_parts.append(f"{duration} min")
        if note:
            detail_parts.append(f"({note})")
        detail = ", ".join(detail_parts) or "logged"
        lines.append(f"- {date_str}: {name} — {detail}")

    history_text = "\n".join(lines)

    prompt = f"""Here are my workouts from the last 30 days:

{history_text}

Give me a brief summary:
1. How many sessions total, and which exercises came up most
2. Am I hitting calisthenics 3x/week on average
3. Anything I should do more or less of

Keep it short and direct."""

    response = claude_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=400,
        system="You are a fitness-aware assistant. Be factual about the data, not falsely encouraging.",
        messages=[{"role": "user", "content": prompt}]
    )

    return response.content[0].text


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
        "How about journaling? How are you feeling mentally after the day?"
        "If you're able, get started on your night routine and start winding down."
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
    },
    {
        "name": "log_workout",
        "description": "Logs a workout session with exercise name and optional sets, reps, and duration. Use this whenever the user mentions doing any physical exercise — calisthenics, a walk, a run, stretching, anything.",
        "input_schema": {
            "type": "object",
            "properties": {
                "exercise": {
                    "type": "string",
                    "description": "The exercise name in the user's own words — e.g. 'pushups', 'pull-ups', 'walk', 'squats'"
                },
                "sets": {
                    "type": "integer",
                    "description": "Number of sets, if mentioned"
                },
                "reps_per_set": {
                    "type": "integer",
                    "description": "Reps per set, if mentioned"
                },
                "duration_minutes": {
                    "type": "integer",
                    "description": "Duration in minutes, if mentioned"
                },
                "note": {
                    "type": "string",
                    "description": "Any extra context the user mentioned, e.g. 'felt strong today' or 'modified, on knees'"
                }
            },
            "required": ["exercise"]
        }
    },
    {
        "name": "get_workout_summary",
        "description": "Pulls the last 30 days of workout history and summarizes frequency, variety, and patterns. Use this when the user asks how their training is going, how often they've been working out, or similar.",
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
    elif tool_name == "log_workout":
        canonical_name = log_workout(
            chat_id,
            tool_input["exercise"],
            sets=tool_input.get("sets"),
            reps_per_set=tool_input.get("reps_per_set"),
            duration_minutes=tool_input.get("duration_minutes"),
            note=tool_input.get("note")
        )
        return f"Logged: {canonical_name}"

    elif tool_name == "get_workout_summary":
        return get_workout_summary(chat_id)

    return None  # not a Wellness tool