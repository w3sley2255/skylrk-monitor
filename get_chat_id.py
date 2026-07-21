#!/usr/bin/env python3
"""Find your Telegram chat ID from the bot's recent messages.

Requires TELEGRAM_BOT_TOKEN in .env and that you have already sent your bot at
least one message (a bot cannot see you until you talk to it first).

    python get_chat_id.py            # print the chat id(s) it can see
    python get_chat_id.py --write    # also fill TELEGRAM_CHAT_ID into .env

Exit codes: 0 found, 1 no messages yet, 2 token missing/invalid.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

ENV_PATH = Path(__file__).with_name(".env")


def write_chat_id(chat_id: str) -> bool:
    """Fill the blank TELEGRAM_CHAT_ID line in .env. Leaves a set value alone."""
    if not ENV_PATH.exists():
        return False
    text = ENV_PATH.read_text(encoding="utf-8")
    new, n = re.subn(r"^TELEGRAM_CHAT_ID=\s*$", f"TELEGRAM_CHAT_ID={chat_id}",
                     text, count=1, flags=re.MULTILINE)
    if n == 0:
        return False
    ENV_PATH.write_text(new, encoding="utf-8")
    return True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--write", action="store_true",
                    help="Write the chat id into .env if that line is still blank")
    args = ap.parse_args(argv)

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        print("TELEGRAM_BOT_TOKEN is not set.\n"
              "Put your BotFather token in .env first (step 2).", file=sys.stderr)
        return 2

    try:
        r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=20)
    except requests.RequestException as exc:
        print(f"Could not reach Telegram: {exc}", file=sys.stderr)
        return 2

    if r.status_code == 401:
        print("Telegram rejected the token (401 Unauthorized).\n"
              "Re-copy it from BotFather - it is easy to truncate.", file=sys.stderr)
        return 2
    if r.status_code != 200:
        print(f"Telegram returned HTTP {r.status_code}: {r.text[:200]}", file=sys.stderr)
        return 2

    # Collect distinct chats across whatever update types came back.
    chats: dict[str, str] = {}
    for upd in r.json().get("result", []):
        for key in ("message", "edited_message", "channel_post", "my_chat_member"):
            chat = (upd.get(key) or {}).get("chat")
            if chat:
                name = chat.get("username") or chat.get("first_name") or chat.get("title") or "?"
                chats[str(chat["id"])] = f"{name} ({chat.get('type', '?')})"

    if not chats:
        print("The bot has not received any messages yet.\n\n"
              "Open the t.me/<your_bot> link from BotFather, press Start, send it\n"
              "any message, then run this again.\n\n"
              "(Telegram only keeps recent updates - if you messaged it a while\n"
              " ago, just send another message.)", file=sys.stderr)
        return 1

    print("Chat ID(s) this bot can see:\n")
    for cid, who in chats.items():
        print(f"  {cid}   <- {who}")

    if args.write:
        if len(chats) > 1:
            print("\nMore than one chat found - not guessing. "
                  "Paste the right one into .env yourself.")
            return 0
        cid = next(iter(chats))
        if write_chat_id(cid):
            print(f"\nWrote TELEGRAM_CHAT_ID={cid} into .env")
            print("Next: python send_test_alert.py")
        else:
            print(f"\nTELEGRAM_CHAT_ID already has a value in .env - left it alone.\n"
                  f"If it is wrong, set it to {cid} manually.")
    else:
        print("\nPaste that number into TELEGRAM_CHAT_ID in .env, "
              "or re-run with --write to do it automatically.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
