from fastapi import FastAPI, Request
from dotenv import load_dotenv
import os
import requests
import anthropic

load_dotenv()

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

@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()
    
    # Extract the message
    message = data.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    user_text = message.get("text", "")
    
    # Skip if no text (could be a photo, sticker, etc.)
    if not user_text or not chat_id:
        return {"ok": True}
    
    # Send to Claude
    response = claude_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system="You are a personal executive assistant. Be concise and practical.",
        messages=[
            {"role": "user", "content": user_text}
        ]
    )
    
    reply = response.content[0].text
    
    # Send Claude's reply back to Telegram
    send_telegram_message(chat_id, reply)
    
    return {"ok": True}