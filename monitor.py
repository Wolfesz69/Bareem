
import os
import json
import re
import time
import hashlib
import html
from pathlib import Path

import requests
import feedparser

ACCOUNTS_FILE = Path("accounts.json")
STATE_FILE = Path("seen.json")

IMPORTANT_WORDS = (
    "مرسوم", "سلطاني", "قانون", "تشريع",
    "قرار وزاري", "قرار حكومي", "إعفاء",
    "تحذير", "تنبيه", "حالة مدارية",
    "منخفض جوي", "أمطار غزيرة",
    "تعليق الدراسة", "إجازة رسمية",
    "إنجاز وطني", "إنجاز عالمي",
    "توجيهات سامية", "مجلس الوزراء",
    "عاجل"
)

LOCAL_WORDS = (
    "البريمي", "محضة", "السنينة",
    "buraimi", "mahdah", "sunaynah"
)


def clean(value):
    value = re.sub(r"<[^>]*>", " ", value or "")
    return re.sub(
        r"\s+", " ", html.unescape(value)
    ).strip()


def get_feed(username, instances):
    for instance in instances:
        url = f"{instance.rstrip('/')}/{username}/rss"

        try:
            response = requests.get(
                url,
                timeout=18,
                headers={
                    "User-Agent": "Mozilla/5.0"
                }
            )
            response.raise_for_status()

            feed = feedparser.parse(response.content)

            if feed.entries:
                print(
                    f"OK: @{username} via {instance}"
                )
                return feed.entries

        except Exception as error:
            print(f"Failed: {url}: {error}")

    print(f"No working feed for @{username}")
    return None


def should_send(text, scope):
    text = text.lower()

    if scope == "local":
        return True

    if any(word in text for word in LOCAL_WORDS):
        return True

    return any(
        word in text for word in IMPORTANT_WORDS
    )


def send_telegram(message):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]

    response = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": message[:4000],
            "disable_web_page_preview": True
        },
        timeout=20
    )
    response.raise_for_status()

    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(str(result))


def main():
    if not os.environ.get("TELEGRAM_BOT_TOKEN"):
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN")

    if not os.environ.get("TELEGRAM_CHAT_ID"):
        raise RuntimeError("Missing TELEGRAM_CHAT_ID")

    config = json.loads(
        ACCOUNTS_FILE.read_text(encoding="utf-8")
    )

    if STATE_FILE.exists():
        state = json.loads(
            STATE_FILE.read_text(encoding="utf-8")
        )
    else:
        state = {"accounts": {}}

    account_states = state.setdefault("accounts", {})
    notifications = 0

    for account in config["accounts"]:
        username = account["username"]
        name = account["name"]
        scope = account["scope"]

        entries = get_feed(
            username,
            config["nitter_instances"]
        )

        if entries is None:
            continue

        previous = account_states.get(username)

        if previous is None:
            # First successful check: save existing posts
            # without sending old notifications.
            ids = [
                hashlib.sha256(
                    (entry.get("id") or
                     entry.get("link") or
                     entry.get("title", "")).encode()
                ).hexdigest()
                for entry in entries
            ]

            account_states[username] = ids[-500:]
            print(f"Initialized @{username}")
            continue

        seen = set(previous)

        for entry in reversed(entries):
            post_id = hashlib.sha256(
                (entry.get("id") or
                 entry.get("link") or
                 entry.get("title", "")).encode()
            ).hexdigest()

            if post_id in seen:
                continue

            title = clean(entry.get("title", ""))
            summary = clean(entry.get("summary", ""))
            link = entry.get("link", "")

            content = title + " " + summary

            if should_send(content, scope):
                category = (
                    "📍 أخبار محافظة البريمي"
                    if scope == "local"
                    else "🇴🇲 أخبار سلطنة عُمان"
                )

                message = (
                    f"{category}\n\n"
                    f"📢 {name}\n"
                    f"@{username}\n\n"
                    f"{title}\n\n"
                    f"🔗 {link}"
                )

                try:
                    send_telegram(message)
                    notifications += 1
                    time.sleep(1)
                except Exception as error:
                    print(f"Telegram error: {error}")
                    # Keep post pending for the next run.
                    continue

            seen.add(post_id)

        account_states[username] = list(seen)[-500:]

    STATE_FILE.write_text(
        json.dumps(
            state,
            ensure_ascii=False,
            indent=2
        ),
        encoding="utf-8"
    )

    print(f"Notifications sent: {notifications}")


if __name__ == "__main__":
    main()
