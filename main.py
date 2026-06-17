from fastapi import FastAPI, Request
from dotenv import load_dotenv
import os
import requests
import anthropic
import logging

logger = logging.getLogger(__name__)

load_dotenv()

app = FastAPI()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

@app.on_event("startup")
async def startup_event():
    logger.info(f"TELEGRAM_TOKEN present: {bool(TELEGRAM_TOKEN)}")
    logger.info(f"ANTHROPIC_KEY present: {bool(os.getenv('ANTHROPIC_API_KEY'))}")

claude_client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

def send_telegram_message(chat_id: str, text: str):
    requests.post(f"{TELEGRAM_URL}/sendMessage", json={
        "chat_id": chat_id,
        "text": text
    })

@app.get("/")
def health_check():
    return {"status": "alive"}

@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()
    message = data.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    user_text = message.get("text", "")
    
    if not user_text or not chat_id:
        return {"ok": True}
    
    response = claude_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system="You are a personal executive assistant. Be concise and practical.",
        messages=[{"role": "user", "content": user_text}]
    )
    
    reply = response.content[0].text
    send_telegram_message(chat_id, reply)
    
    return {"ok": True}