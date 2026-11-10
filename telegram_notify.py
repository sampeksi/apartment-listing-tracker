#!/usr/bin/env python3
"""Telegram delivery for the business flow's investment analysis digest.

Needs TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID set (GitHub secrets - see
README), no hardcoded defaults for either.

Uses classic Markdown (not MarkdownV2) deliberately: MarkdownV2 requires
escaping most punctuation (., -, (, ), !, ...), which auto-generated text
full of prices/percentages would constantly trip over. Classic Markdown's
much smaller escape set is a better fit for that kind of content.
"""
import os

import requests

TELEGRAM_MESSAGE_LIMIT = 4096


def send_message(text: str, parse_mode: str = "Markdown") -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise SystemExit("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID environment variables must be set")

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for chunk in _chunk_text(text, TELEGRAM_MESSAGE_LIMIT):
        resp = requests.post(
            url, json={"chat_id": chat_id, "text": chunk, "parse_mode": parse_mode}, timeout=15
        )
        resp.raise_for_status()


def _chunk_text(text: str, limit: int) -> list[str]:
    """Telegram caps messages at 4096 chars - split on paragraph/line
    boundaries rather than cutting mid-sentence or silently truncating."""
    if len(text) <= limit:
        return [text]
    chunks = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        split_at = remaining.rfind("\n\n", 0, limit)
        if split_at == -1:
            split_at = remaining.rfind("\n", 0, limit)
        if split_at == -1:
            split_at = limit
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:].lstrip("\n")
    return chunks
