"""Post league chat directly, or prepare browser-assisted posts and verify their delivery."""

import time

from .client import cache_mode


def prepare(manager, league_id, text, request_id):
    league = manager.config.league(league_id)
    if not league.chat_enabled:
        raise ValueError("Chat is disabled for this league")
    if not text.strip() or len(text) > 2000 or not request_id or len(request_id) > 100:
        raise ValueError(
            "Use a message of 1 to 2000 characters and a request ID of 1 to 100 characters"
        )
    db = manager.store.db
    with db:
        db.execute(
            "INSERT OR IGNORE INTO chat_posts VALUES(?,?,?,?,?,?)",
            (request_id, league_id, text, time.time(), "prepared", None),
        )
    row = db.execute("SELECT * FROM chat_posts WHERE request_id=?", (request_id,)).fetchone()
    if row[1] != league_id or row[2] != text:
        raise ValueError("Request ID already belongs to a different message or league")
    return {
        "request_id": request_id,
        "league_id": league_id,
        "league_name": league.label,
        "text": text,
        "status": row[4],
        "message_id": row[5],
        "url": f"https://sleeper.com/leagues/{league_id}/matchup",
        "delivery_method": "browser_assisted",
        "sent_by_this_tool": False,
        "next_step": "After user authorization, claim this request before a single browser submission.",
    }


def claim(manager, request_id):
    row = manager.store.db.execute(
        "SELECT league FROM chat_posts WHERE request_id=?", (request_id,)
    ).fetchone()
    if not row:
        raise ValueError("Unknown request ID")
    manager.config.league(row[0])
    with manager.store.db:
        changed = manager.store.db.execute(
            "UPDATE chat_posts SET status='unknown' WHERE request_id=? AND status='prepared'",
            (request_id,),
        ).rowcount
    return {
        "request_id": request_id,
        "may_submit_once": changed == 1,
        "status": "unknown",
        "warning": "Do not retry submission after a timeout. Verify delivery first.",
    }


async def verify(manager, request_id):
    row = manager.store.db.execute(
        "SELECT * FROM chat_posts WHERE request_id=?", (request_id,)
    ).fetchone()
    if not row:
        raise ValueError("Unknown request ID")
    _, league_id, text, created, status, message_id = row
    manager.config.league(league_id)
    if status in {"confirmed", "prepared"}:
        return {"request_id": request_id, "status": status, "message_id": message_id}
    marker = cache_mode.set("refresh")
    try:
        result = await manager.chat(league_id, limit=100)
    finally:
        cache_mode.reset(marker)
    matches = [
        m
        for m in result.get("data") or []
        if str(m.get("author_id")) == manager.config.user_id
        and m.get("text") == text
        and isinstance(m.get("created"), (int, float))
        and m["created"] >= created * 1000
        and m.get("message_id")
    ]
    if not result.get("partial") and not result.get("stale") and len(matches) == 1:
        message_id = str(matches[0]["message_id"])
        with manager.store.db:
            manager.store.db.execute(
                "UPDATE chat_posts SET status='confirmed', message_id=? WHERE request_id=?",
                (message_id, request_id),
            )
        status = "confirmed"
    return {
        "request_id": request_id,
        "status": status,
        "message_id": message_id,
        "errors": result.get("errors", []),
        "verification": "author_text_time_match",
        "automatic_retry_allowed": False,
    }


async def send(manager, league_id, text):
    league = manager.config.league(league_id)
    if not league.chat_enabled:
        raise ValueError("Chat is disabled for this league")
    if not text.strip() or len(text) > 2000:
        raise ValueError("Use a message of 1 to 2000 characters")
    result = await manager.client.create_message(league_id, text)
    message = (result.get("data") or {}).get("create_message") or {}
    message_id = message.get("message_id")
    status = "confirmed" if message_id and str(message.get("parent_id")) == league_id else "unknown"
    with manager.store.db:
        manager.store.db.execute(
            "INSERT OR REPLACE INTO chat_posts VALUES(?,?,?,?,?,?)",
            (result["client_id"], league_id, text, time.time(), status, message_id),
        )
    return {
        "request_id": result["client_id"],
        "league_id": league_id,
        "league_name": league.label,
        "text": text,
        "status": status,
        "message_id": message_id,
        "errors": result.get("errors", []),
        "delivery_method": "graphql_mutation",
        "sent_by_this_tool": True,
        "warning": "Do not retry an unknown result. Use get_chat_post_status to verify first.",
    }
