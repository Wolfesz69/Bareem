
import os
import json
import re
import time
import hashlib
import html

from pathlib import Path
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from calendar import timegm
from urllib.parse import urlparse

import requests
import feedparser


# =====================================
# FILES AND SETTINGS
# =====================================

ACCOUNTS_FILE = Path("accounts.json")
STATE_FILE = Path("seen.json")
LOG_FILE = Path("posts_log.json")

MAX_LOG = 5000
MAX_SEEN_PER_ACCOUNT = 500

# No posts before this date will be sent.
# October 13, 2026 - midnight Oman time.

CUTOFF = datetime(
    2026, 10, 13, 0, 0,
    tzinfo=ZoneInfo("Asia/Muscat")
).astimezone(timezone.utc)


# =====================================
# NEWS FILTERS
# =====================================

IMPORTANT_WORDS = (
    "مرسوم",
    "سلطاني",
    "قانون",
    "تشريع",
    "قرار وزاري",
    "قرار حكومي",
    "إعفاء",
    "تحذير",
    "تنبيه",
    "حالة مدارية",
    "منخفض جوي",
    "أمطار غزيرة",
    "تعليق الدراسة",
    "إجازة رسمية",
    "إنجاز وطني",
    "إنجاز عالمي",
    "توجيهات سامية",
    "مجلس الوزراء",
    "عاجل",
    "royal decree",
    "official warning",
    "breaking news",
    "cabinet decision"
)

LOCAL_WORDS = (
    "البريمي",
    "محضة",
    "السنينة",
    "buraimi",
    "mahdah",
    "mahdha",
    "sunaynah",
    "sunanah"
)


# =====================================
# HELPERS
# =====================================

def clean(value):
    value = re.sub(r"<[^>]*>", " ", value or "")

    return re.sub(
        r"\s+",
        " ",
        html.unescape(value)
    ).strip()


def load_json(path, default):
    if not path.exists():
        return default

    return json.loads(
        path.read_text(encoding="utf-8")
    )


def save_json(path, data):
    path.write_text(
        json.dumps(
            data,
            ensure_ascii=False,
            indent=2
        ),
        encoding="utf-8"
    )


def post_hash(entry):
    value = (
        entry.get("id")
        or entry.get("link")
        or entry.get("title", "")
    )

    return hashlib.sha256(
        value.encode("utf-8")
    ).hexdigest()


# =====================================
# PUBLICATION DATE CHECK
# =====================================

def get_post_date(entry):
    published = (
        entry.get("published_parsed")
        or entry.get("updated_parsed")
    )

    if not published:
        return None

    try:
        timestamp = timegm(published)

        return datetime.fromtimestamp(
            timestamp,
            tz=timezone.utc
        )

    except (TypeError, ValueError, OverflowError):
        return None


def is_recent_post(entry):
    post_date = get_post_date(entry)

    if post_date is None:
        return False

    now = datetime.now(timezone.utc)

    # Never send old posts or future-dated posts.
    return CUTOFF <= post_date <= now


# =====================================
# ORIGINAL X POST LINK
# =====================================

def original_url(entry, username):
    link = entry.get("link", "")

    parsed = urlparse(link)

    match = re.search(
        r"/status/(\d+)",
        parsed.path
    )

    if match:
        return (
            f"https://x.com/{username}/status/"
            f"{match.group(1)}"
        )

    return link


# =====================================
# FETCH RSS FEED
# =====================================

def get_feed(username, instances):
    for instance in instances:
        url = (
            f"{instance.rstrip('/')}/"
            f"{username}/rss"
        )

        try:
            response = requests.get(
                url,
                timeout=18,
                headers={
                    "User-Agent": "Mozilla/5.0"
                }
            )

            response.raise_for_status()

            feed = feedparser.parse(
                response.content
            )

            if feed.entries:
                print(
                    f"OK: @{username} via {instance}"
                )
                return feed.entries

        except Exception as error:
            print(
                f"Failed: {url}: {error}"
            )

    print(
        f"No working feed for @{username}"
    )

    return None


# =====================================
# FILTER NEWS
# =====================================

def should_send(text, scope):
    text = text.lower()

    if scope == "local":
        return True

    if any(
        word in text
        for word in LOCAL_WORDS
    ):
        return True

    return any(
        word in text
        for word in IMPORTANT_WORDS
    )


# =====================================
# TELEGRAM
# =====================================

def send_telegram(message):
    token = os.environ[
        "TELEGRAM_BOT_TOKEN"
    ]

    chat_id = os.environ[
        "TELEGRAM_CHAT_ID"
    ]

    response = requests.post(
        (
            "https://api.telegram.org/"
            f"bot{token}/sendMessage"
        ),
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


# =====================================
# POSTS LOG
# =====================================

def make_record(entry, account, status):
    title = clean(
        entry.get("title", "")
    )

    summary = clean(
        entry.get("summary", "")
    )

    post_date = get_post_date(entry)

    return {
        "id": post_hash(entry),
        "account": account["username"],
        "source": account["name"],
        "scope": account["scope"],
        "title": title,
        "text": summary or title,
        "url": original_url(
            entry,
            account["username"]
        ),
        "published": (
            post_date.isoformat()
            if post_date
            else None
        ),
        "detected_at": datetime.now(
            timezone.utc
        ).isoformat(),
        "status": status,
        "sent_to_telegram": status == "sent"
    }


# =====================================
# MAIN MONITOR
# =====================================

def main():
    if not os.environ.get(
        "TELEGRAM_BOT_TOKEN"
    ):
        raise RuntimeError(
            "Missing TELEGRAM_BOT_TOKEN"
        )

    if not os.environ.get(
        "TELEGRAM_CHAT_ID"
    ):
        raise RuntimeError(
            "Missing TELEGRAM_CHAT_ID"
        )

    config = load_json(
        ACCOUNTS_FILE,
        {}
    )

    state = load_json(
        STATE_FILE,
        {"accounts": {}}
    )

    log = load_json(
        LOG_FILE,
        []
    )

    account_states = state.setdefault(
        "accounts",
        {}
    )

    logged_ids = {
        record["id"]
        for record in log
        if "id" in record
    }

    notifications = 0
    new_records = 0
    skipped_old = 0

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

        previous = account_states.get(
            username
        )

        # First check for a newly added account:
        # Record posts, but don't send old ones.
        if previous is None:
            account_states[username] = [
                post_hash(entry)
                for entry in entries
            ][-MAX_SEEN_PER_ACCOUNT:]

            for entry in reversed(entries):
                record = make_record(
                    entry,
                    account,
                    "initial"
                )

                if record["id"] not in logged_ids:
                    log.append(record)
                    logged_ids.add(
                        record["id"]
                    )
                    new_records += 1

            print(
                f"Initialized @{username}"
            )
            continue

        seen = set(previous)

        for entry in reversed(entries):
            pid = post_hash(entry)

            if pid in seen:
                continue

            # IMPORTANT:
            # Never send posts before cutoff.
            if not is_recent_post(entry):
                seen.add(pid)
                skipped_old += 1

                if pid not in logged_ids:
                    record = make_record(
                        entry,
                        account,
                        "skipped_old"
                    )

                    log.append(record)
                    logged_ids.add(pid)
                    new_records += 1

                continue

            title = clean(
                entry.get("title", "")
            )

            summary = clean(
                entry.get("summary", "")
            )

            content = (
                title + " " + summary
            )

            eligible = should_send(
                content,
                scope
            )

            status = "filtered"

            if eligible:
                is_local = (
                    scope == "local"
                    or any(
                        word in content.lower()
                        for word in LOCAL_WORDS
                    )
                )

                category = (
                    "📍 أخبار محافظة البريمي"
                    if is_local
                    else "🇴🇲 أخبار سلطنة عُمان"
                )

                url = original_url(
                    entry,
                    username
                )

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
                        f"Telegram error "
                        f"@{username}: {error}"
                    )

                    # Retry next run.
                    continue

            if pid not in logged_ids:
                log.append(
                    make_record(
                        entry,
                        account,
                        status
                    )
                )

                logged_ids.add(pid)
                new_records += 1

            seen.add(pid)

        account_states[username] = list(
            seen
        )[-MAX_SEEN_PER_ACCOUNT:]

    save_json(
        STATE_FILE,
        state
    )

    save_json(
        LOG_FILE,
        log[-MAX_LOG:]
    )

    print(
        f"Notifications sent: {notifications}"
    )

    print(
        f"Old posts skipped: {skipped_old}"
    )

    print(
        f"New log records: {new_records}"
    )

    print(
        f"Total log records: "
        f"{min(len(log), MAX_LOG)}"
    )


if __name__ == "__main__":
    main()
