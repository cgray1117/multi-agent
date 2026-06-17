"""
telegram.py

Tiny module that just knows how to send a message to Telegram.
Every agent imports send_telegram_message from here instead of
duplicating the requests.post call in multiple files.
"""

import os
import requests

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"


def send_telegram_message(chat_id: str, text: str):
    """
    Sends a text message to a specific Telegram chat.
    chat_id tells Telegram WHICH conversation to deliver it to.
    """
    requests.post(f"{TELEGRAM_URL}/sendMessage", json={
        "chat_id": chat_id,
        "text": text
    })