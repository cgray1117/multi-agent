from fastapi import FastAPI, Request
from dotenv import load_dotenv
import os
import requests
import anthropic
import logging

from sqlalchemy import create_engine, text

DATABASE_URL = os.getenv("DATABASE_URL")
engine = create_engine(DATABASE_URL)

def create_tables():
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
                created_at TIMESTAMP DEFAULT NOW()
            )
        """))
        conn.commit()

create_tables()

logger = logging.getLogger(__name__)

#load_dotenv()

app = FastAPI()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

claude_client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

def send_telegram_message(chat_id: str, text: str):
    requests.post(f"{TELEGRAM_URL}/sendMessage", json={
        "chat_id": chat_id,
        "text": text
    })

@app.get("/")
def health_check():
    return {"status": "alive"}

def get_conversation_history(chat_id):
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
    with engine.connect() as conn:
        conn.execute(text("""
            INSERT INTO messages (chat_id, role, content)
            VALUES (:chat_id, :role, :content)
        """), {"chat_id": chat_id, "role": role, "content": content})
        conn.commit()

@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()
    message = data.get("message", {})
    chat_id = str(message.get("chat", {}).get("id"))
    user_text = message.get("text", "")

    if not user_text or not chat_id:
        return {"ok": True}

    # Save user message
    save_message(chat_id, "user", user_text)

    # Get conversation history
    history = get_conversation_history(chat_id)

    # Send to Claude with full history
    response = claude_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system="You are a personal executive assistant. Be concise and practical.",
        messages=history
    )

    reply = response.content[0].text

    # Save Claude's reply
    save_message(chat_id, "assistant", reply)

    send_telegram_message(chat_id, reply)

    return {"ok": True}