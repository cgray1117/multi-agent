"""
database.py

Holds the single shared SQLAlchemy engine and the table creation logic.
Every other file imports `engine` from here instead of creating its own
connection — there should only ever be ONE engine in the whole app.
"""

import os
from sqlalchemy import create_engine, text

# Pull the database connection string from environment variables
# (set in .env locally, and in Railway's Variables tab in production)
DATABASE_URL = os.getenv("DATABASE_URL")

# Create a SQLAlchemy "engine" — manages the pool of connections to
# Postgres. Every file that needs to query the DB imports this object.
engine = create_engine(DATABASE_URL)


def create_tables():
    """
    Creates all tables for all agents if they don't already exist.
    Safe to run every time the app starts — IF NOT EXISTS means it
    won't wipe or duplicate tables on restart/redeploy.

    Kept centralized here (rather than split per-agent) so there's
    one place to look when checking what tables exist across the
    whole system.
    """
    with engine.connect() as conn:

        # --- Shared / Chief of Staff tables ---

        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS messages (
                id SERIAL PRIMARY KEY,
                chat_id TEXT NOT NULL,
                role TEXT NOT NULL,          -- 'user' or 'assistant'
                content TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """))

        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS tasks (
                id SERIAL PRIMARY KEY,
                chat_id TEXT NOT NULL,
                task TEXT NOT NULL,
                status TEXT DEFAULT 'open',  -- 'open' or 'done'
                due_date DATE,
                priority TEXT DEFAULT 'normal',  -- 'low', 'normal', 'high'
                created_at TIMESTAMP DEFAULT NOW()
            )
        """))

        # --- Career agent tables ---

        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS clients (
                id SERIAL PRIMARY KEY,
                chat_id TEXT NOT NULL,
                name TEXT NOT NULL,
                stage TEXT DEFAULT 'prospect',  -- prospect, proposal, active, closed
                notes TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """))

        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS revenue (
                id SERIAL PRIMARY KEY,
                chat_id TEXT NOT NULL,
                amount NUMERIC NOT NULL,
                source TEXT,            -- e.g. client name or project
                logged_at TIMESTAMP DEFAULT NOW()
            )
        """))

        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS learning_goals (
                id SERIAL PRIMARY KEY,
                chat_id TEXT NOT NULL,
                skill TEXT NOT NULL,
                target_date DATE,
                status TEXT DEFAULT 'in_progress',  -- in_progress, done
                created_at TIMESTAMP DEFAULT NOW()
            )
        """))

        # --- Wellness + Personal agent tables (shared schema) ---
        # One flexible system covers both agents: physical/mental health
        # habits (Wellness) and self-care/cleanliness/social habits
        # (Personal) are structurally identical — a recurring action
        # with a target frequency and a log of when it happened.

        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS habits (
                id SERIAL PRIMARY KEY,
                chat_id TEXT NOT NULL,
                category TEXT NOT NULL,        -- 'physical', 'mental', 'self_care', 'cleanliness', 'social'
                name TEXT NOT NULL,            -- e.g. 'calisthenics', 'daily walk', 'therapy'
                target_frequency TEXT,         -- e.g. '3x/week', 'daily', 'biweekly', 'weekly'
                created_at TIMESTAMP DEFAULT NOW()
            )
        """))

        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS habit_logs (
                id SERIAL PRIMARY KEY,
                chat_id TEXT NOT NULL,
                habit_id INTEGER REFERENCES habits(id),
                logged_at TIMESTAMP DEFAULT NOW(),
                note TEXT                      -- optional context, e.g. "skipped, low energy"
            )
        """))

        conn.commit()