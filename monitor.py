
import os
import json
import re
import time
import hashlib
import html
from pathlib import Path
from datetime import datetime, timezone
from zoneinfo import ZoneInfo



import requests
import feedparser

ACCOUNTS_FILE = Path("accounts.json")
STATE_FILE = Path("seen.json")
LOG_FILE = Path("posts_log.json")
MAX_LOG = 5000

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
    "buraimi", "mahdah", "sunaynah",
    "mahdha", "sunanah"
)


def clean(value):
    value = re.sub(r"<[^>]*>", " ", value or "")
    return re.sub(
        r"\s+", " ", html.unescape(value)
    ).strip()


def load_json(path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path, data):
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )


def get_feed(username, instances):
    for instance in instances:
        url = f"{instance.rstrip('/')}/{username}/rss"
        try:
            response = requests.get(
                url,
                timeout=18,
                headers={"User-Agent": "Mozilla/5.0"}
            )
            response.raise_for_status()
            feed = feedparser.parse(response.content)

            if feed.entries:
                print(f"OK: @{username} via {instance}")
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

    return any(word in text for word in IMPORTANT_WORDS)


def original_url(entry, username):
    link = entry.get("link", "")
    match = re.search(
        r"/(?:[^/]+/)?status/(\d+)",
        link
    )

    if match:
        return f"https://x.com/{username}/status/{match.group(1)}"

    return link


def post_hash(entry):
    value = (
        entry.get("id")
        or entry.get("link")
        or entry.get("title", "")
    )
    return hashlib.sha256(value.encode()).hexdigest()


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


def make_record(entry, account, status):
    title = clean(entry.get("title", ""))
    summary = clean(entry.get("summary", ""))

    return {
        "id": post_hash(entry),
        "account": account["username"],
        "source": account["name"],
        "scope": account["scope"],
        "title": title,
        "text": summary or title,
        "url": original_url(
            entry, account["username"]
        ),
        "published": entry.get("published", ""),
        "detected_at": datetime.now(
            timezone.utc
        ).isoformat(),
        "status": status,
        "sent_to_telegram": status == "sent"
    }


# Only accept posts published on or after
# 13 October 2026, 00:00 Oman time.
CUTOFF = datetime(
    2026, 10, 13, 0, 0,
    tzinfo=ZoneInfo("Asia/Muscat")
).astimezone(timezone.utc)


def is_recent_post(entry):
    published = (
        entry.get("published_parsed")
        or entry.get("updated_parsed")
    )

    if not published:
        # Unknown date: do not send.
        return False

    post_date = datetime(
        *published[:6],
        tzinfo=timezone.utc
    )

    return post_date >= CUTOFF

def main():
    if not os.environ.get("TELEGRAM_BOT_TOKEN"):
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN")

    if not os.environ.get("TELEGRAM_CHAT_ID"):
        raise RuntimeError("Missing TELEGRAM_CHAT_ID")

    config = load_json(ACCOUNTS_FILE, {})
    state = load_json(STATE_FILE, {"accounts": {}})
    log = load_json(LOG_FILE, [])

    account_states = state.setdefault("accounts", {})
    logged_ids = {
        record["id"] for record in log
        if "id" in record
    }

    notifications = 0
    new_records = 0

    for account in config["accounts"]:
        username = account["username"]
        name = account["name"]
        scope = account["scope"]

        entries = get_feed(
            username, config["nitter_instances"]
        )

        if entries is None:
            continue

        previous = account_states.get(username)

        if previous is None:
            account_states[username] = [
                post_hash(entry) for entry in entries
            ][-500:]

            # Record existing posts, but do not
            # send old notifications on first check.
            for entry in reversed(entries):
                record = make_record(
                    entry, account, "initial"
                )
                if record["id"] not in logged_ids:
                    log.append(record)
                    logged_ids.add(record["id"])
                    new_records += 1

            print(f"Initialized @{username}")
            continue

        seen = set(previous)

        for entry in reversed(entries):
            pid = post_hash(entry)

            if pid in seen:
                continue
                
            if not is_recent_post(entry):
                seen.add(pid)
                continue


            title = clean(entry.get("title", ""))
            summary = clean(entry.get("summary", ""))
            content = title + " " + summary

            eligible = should_send(content, scope)
            status = "filtered"

            if eligible:
                category = (
                    "📍 أخبار محافظة البريمي"
                    if scope == "local"
                    or any(
                        word in content.lower()
                        for word in LOCAL_WORDS
                    )
                    else "🇴🇲 أخبار سلطنة عُمان"
                )

                url = original_url(entry, username)
                message = (
                    f"{category}\n\n"
                    f"📢 {name}\n"
                    f"@{username}\n\n"
                    f"{title}\n\n"
                    f"🔗 {url}"
                )

                try:
                    send_telegram(message)
                    notifications += 1
                    status = "sent"
                    time.sleep(1)
                except Exception as error:
                    print(
                        f"Telegram error @{username}: {error}"
                    )
                    # Retry this post on next run.
                    continue

            if pid not in logged_ids:
                log.append(
                    make_record(entry, account, status)
                )
                logged_ids.add(pid)
                new_records += 1

            seen.add(pid)

        account_states[username] = list(seen)[-500:]

    save_json(STATE_FILE, state)
    save_json(LOG_FILE, log[-MAX_LOG:])

    print(f"Notifications sent: {notifications}")
    print(f"New log records: {new_records}")
    print(f"Total log records: {min(len(log), MAX_LOG)}")


if __name__ == "__main__":
    main()
