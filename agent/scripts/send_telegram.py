#!/usr/bin/env python3
"""
Omni Agent — Standalone Telegram Message Sender

Sends a message to the owner's Telegram chat. Used by:
- launchd cron jobs: claude -p "/briefing" | python3 scripts/send_telegram.py
- intelligence.py for urgent alerts
- Any script that needs to push a notification

Usage:
    echo "Hello" | python3 scripts/send_telegram.py
    python3 scripts/send_telegram.py "Direct message text"
    python3 scripts/send_telegram.py --file /path/to/message.txt
"""

import os
import re
import sys
import time
import argparse
from pathlib import Path

import requests
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
# ONE DOOR (2026-10-15): this script is allowed without an Omni approval ONLY
# because it can reach nobody but the owner. The destination is pinned to his own
# chat; a TELEGRAM_CHAT_ID pointing anywhere else (an env prefix an agent could
# type) is ignored. Messages to other people are proposed in Omni instead.
#: Config: OMNI_OWNER_TELEGRAM_CHAT (The owner's own chat id). Empty = this script sends nothing.
OWNER_CHAT_ID = os.getenv("OMNI_OWNER_TELEGRAM_CHAT", "").strip()
if OWNER_CHAT_ID and os.getenv("TELEGRAM_CHAT_ID", OWNER_CHAT_ID).strip() != OWNER_CHAT_ID:
    print("send_telegram: TELEGRAM_CHAT_ID is not the owner's own chat; using his chat only.",
          file=sys.stderr)
CHAT_ID = OWNER_CHAT_ID
MAX_MSG_LEN = 4096


def md_to_telegram_html(text: str) -> str:
    """Convert markdown to Telegram-compatible HTML.

    Order matters: escape HTML entities first, then convert markdown
    constructs from most specific (code blocks) to least (italic).
    """
    import html as html_mod
    # 1. Protect code blocks
    code_blocks = []
    def _save_code_block(m):
        code = html_mod.escape(m.group(2).strip())
        code_blocks.append(f"<pre>{code}</pre>")
        return f"\x00CODEBLOCK{len(code_blocks) - 1}\x00"
    text = re.sub(r'```(\w*)\n?([\s\S]*?)```', _save_code_block, text)

    # 2. Protect inline code
    inline_codes = []
    def _save_inline(m):
        code = html_mod.escape(m.group(1))
        inline_codes.append(f"<code>{code}</code>")
        return f"\x00INLINE{len(inline_codes) - 1}\x00"
    text = re.sub(r'`([^`]+)`', _save_inline, text)

    # 3. HTML-escape the rest
    text = html_mod.escape(text)

    # 4. Links
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<a href="\2">\1</a>', text)

    # 5. Bold
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'__(.+?)__', r'<b>\1</b>', text)

    # 6. Italic
    text = re.sub(r'\*(.+?)\*', r'<i>\1</i>', text)
    text = re.sub(r'(?<!\w)_(.+?)_(?!\w)', r'<i>\1</i>', text)

    # 7. Headers → bold
    text = re.sub(r'^#{1,6}\s+(.+)$', r'<b>\1</b>', text, flags=re.MULTILINE)

    # 8. Bullets
    text = re.sub(r'^[\s]*[-*]\s+', '- ', text, flags=re.MULTILINE)

    # 9. Restore protected content
    for i, block in enumerate(code_blocks):
        text = text.replace(f"\x00CODEBLOCK{i}\x00", block)
    for i, code in enumerate(inline_codes):
        text = text.replace(f"\x00INLINE{i}\x00", code)

    return text


def send_message(text: str) -> bool:
    """Send a message to the owner's Telegram. Returns True on success."""
    if not TELEGRAM_BOT_TOKEN:
        print("Error: TELEGRAM_BOT_TOKEN not set in .env", file=sys.stderr)
        return False
    if not CHAT_ID:
        print("Error: OMNI_OWNER_TELEGRAM_CHAT not set; nothing sent", file=sys.stderr)
        return False

    if not text or not text.strip():
        return True  # Nothing to send

    # Convert markdown to Telegram HTML for proper formatting
    text = md_to_telegram_html(text)

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    # Split long messages
    chunks = []
    if len(text) <= MAX_MSG_LEN:
        chunks = [text]
    else:
        current = ""
        for line in text.split("\n"):
            if len(current) + len(line) + 1 > MAX_MSG_LEN:
                chunks.append(current)
                current = line
            else:
                current += ("\n" if current else "") + line
        if current:
            chunks.append(current)

    success = True
    for chunk in chunks:
        max_retries = 3
        for attempt in range(max_retries + 1):
            try:
                resp = requests.post(url, json={
                    "chat_id": CHAT_ID,
                    "text": chunk,
                    "parse_mode": "HTML",
                }, timeout=30)
                if not resp.ok:
                    print(f"Telegram API error: {resp.status_code} {resp.text}", file=sys.stderr)
                    success = False
                break  # Success or non-retryable API error
            except (requests.exceptions.ConnectionError,
                    requests.exceptions.Timeout,
                    requests.exceptions.RequestException) as e:
                if attempt < max_retries:
                    delay = 2 ** (attempt + 1)  # 2s, 4s, 8s
                    print(f"Retry {attempt + 1}/{max_retries} after {delay}s: {e}", file=sys.stderr)
                    time.sleep(delay)
                else:
                    print(f"Failed after {max_retries} retries: {e}", file=sys.stderr)
                    success = False

    return success


def send_document(file_path: str, caption: str = "") -> bool:
    """Send a file to the owner's Telegram as a document attachment.

    Uses Telegram's sendDocument API (multipart/form-data).
    Caption is limited to 1024 chars by Telegram.
    """
    if not TELEGRAM_BOT_TOKEN:
        print("Error: TELEGRAM_BOT_TOKEN not set in .env", file=sys.stderr)
        return False
    if not CHAT_ID:
        print("Error: OMNI_OWNER_TELEGRAM_CHAT not set; nothing sent", file=sys.stderr)
        return False

    path = Path(file_path)
    if not path.exists():
        print(f"Error: file not found: {file_path}", file=sys.stderr)
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument"
    data = {"chat_id": CHAT_ID, "parse_mode": "HTML"}
    if caption:
        data["caption"] = caption[:1024]

    try:
        with open(path, "rb") as f:
            resp = requests.post(url, data=data, files={"document": (path.name, f)}, timeout=60)
        if not resp.ok:
            print(f"Telegram sendDocument error: {resp.status_code} {resp.text}", file=sys.stderr)
            return False
        return True
    except Exception as e:
        print(f"Telegram sendDocument failed: {e}", file=sys.stderr)
        return False


def main():
    parser = argparse.ArgumentParser(description="Send a Telegram message to the owner")
    parser.add_argument("message", nargs="?", help="Message text (or pipe via stdin)")
    parser.add_argument("--file", "-f", help="Read message from file")
    args = parser.parse_args()

    if args.file:
        text = Path(args.file).read_text()
    elif args.message:
        text = args.message
    elif not sys.stdin.isatty():
        text = sys.stdin.read()
    else:
        print("No message provided. Pipe stdin, pass as argument, or use --file.", file=sys.stderr)
        sys.exit(1)

    if send_message(text):
        print("Sent.")
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
