#!/usr/bin/env python3
"""
Flask dashboard for WhatsApp Customer Service Bot.

Receptionist-facing UI for:
- Viewing incoming messages
- Reviewing AI-suggested responses
- Approving/editing/sending responses
- Monitoring auto-replies
"""

import os
import sys
import json
import threading
from pathlib import Path
from datetime import datetime

from flask import Flask, render_template, request, jsonify, redirect, url_for
from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, str(Path(__file__).parent.parent))
from db import (
    init_db, get_pending_messages, get_auto_replied, get_all_messages,
    approve_response, reject_response, mark_sent, store_message, get_db,
)
from responder import generate_response, process_new_messages

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-key-change-me")


@app.before_request
def ensure_db():
    init_db()


# ── Pages ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    """Main dashboard — pending review queue."""
    hours = int(request.args.get("hours", 48))
    pending = get_pending_messages(hours=hours)
    auto_replied = get_auto_replied()
    all_msgs = get_all_messages(limit=50)

    stats = {
        "pending": len([m for m in pending if m.get("status") == "pending_review"]),
        "new": len([m for m in pending if m.get("status") == "new"]),
        "auto_replied": len(auto_replied),
        "total": len(all_msgs),
    }

    return render_template("inbox.html", pending=pending, auto_replied=auto_replied,
                           all_messages=all_msgs, stats=stats, hours=hours)


# ── API endpoints ─────────────────────────────────────────────────────────────

@app.route("/api/messages", methods=["GET"])
def api_messages():
    """Get all messages with responses."""
    messages = get_all_messages(limit=int(request.args.get("limit", 100)))
    return jsonify(messages)


@app.route("/api/pending", methods=["GET"])
def api_pending():
    """Get messages pending review."""
    return jsonify(get_pending_messages())


@app.route("/api/approved-responses", methods=["GET"])
def api_approved_responses():
    """Get approved responses not yet sent — polled by the scanner to auto-send."""
    conn = get_db()
    rows = conn.execute("""
        SELECT r.id as response_id,
               COALESCE(r.final_text, r.suggested_text) as text,
               m.chat_name, m.sender
        FROM responses r
        JOIN messages m ON m.id = r.message_id
        WHERE r.status = 'approved' AND r.sent_at IS NULL
        ORDER BY r.created_at ASC
    """).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/approve/<int:response_id>", methods=["POST"])
def api_approve(response_id):
    """Approve a suggested response (optionally with edits)."""
    data = request.get_json(silent=True) or {}
    final_text = data.get("final_text")
    approve_response(response_id, final_text)
    return jsonify({"ok": True, "response_id": response_id})


@app.route("/api/reject/<int:response_id>", methods=["POST"])
def api_reject(response_id):
    """Reject a suggested response."""
    reject_response(response_id)
    return jsonify({"ok": True, "response_id": response_id})


@app.route("/api/dismiss/<int:message_id>", methods=["POST"])
def api_dismiss(message_id):
    """Permanently delete a message and its responses."""
    conn = get_db()
    conn.execute("DELETE FROM responses WHERE message_id = ?", (message_id,))
    conn.execute("DELETE FROM messages WHERE id = ?", (message_id,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/dismiss-groups", methods=["POST"])
def api_dismiss_groups():
    """Delete all messages that look like they came from group chats."""
    conn = get_db()
    rows = conn.execute("""
        SELECT id FROM messages
        WHERE chat_name LIKE '~ %'
           OR chat_name LIKE '% typing...'
           OR chat_name LIKE '% is typing...'
           OR chat_name LIKE '#%'
           OR (chat_name IS NOT NULL AND chat_name != '' AND chat_name != sender)
    """).fetchall()
    ids = [r["id"] for r in rows]
    if ids:
        placeholders = ",".join("?" * len(ids))
        conn.execute(f"DELETE FROM responses WHERE message_id IN ({placeholders})", ids)
        conn.execute(f"DELETE FROM messages WHERE id IN ({placeholders})", ids)
        conn.commit()
    conn.close()
    return jsonify({"ok": True, "deleted": len(ids)})


@app.route("/api/sent/<int:response_id>", methods=["POST"])
def api_mark_sent(response_id):
    """Mark a response as sent (after receptionist copies to WhatsApp)."""
    mark_sent(response_id)
    return jsonify({"ok": True, "response_id": response_id})


@app.route("/api/regenerate/<int:message_id>", methods=["POST"])
def api_regenerate(message_id):
    """Regenerate AI response for a message."""
    conn = get_db()
    row = conn.execute(
        "SELECT sender, content, chat_name FROM messages WHERE id = ?", (message_id,)
    ).fetchone()
    conn.close()

    if not row:
        return jsonify({"error": "Message not found"}), 404

    result = generate_response(row["sender"], row["content"], row["chat_name"])
    from db import store_response
    resp_id = store_response(
        message_id=message_id,
        suggested_text=result.get("reply", ""),
        confidence=result.get("confidence", 0.0),
        auto_reply=False,  # regenerated = always needs review
    )
    return jsonify({"ok": True, "response_id": resp_id, "response": result})


@app.route("/api/ingest", methods=["POST"])
def api_ingest():
    """Ingest raw messages from the scanner service (internal use).

    Accepts a list of messages, deduplicates, stores, and triggers AI response generation.
    Used when scanner runs as a separate Railway service and can't write to SQLite directly.
    """
    from db import message_exists
    data = request.get_json()
    messages = data.get("messages", [])
    if not messages:
        return jsonify({"error": "messages is required"}), 400

    new_messages = []
    for msg in messages:
        sender = msg.get("sender", "Unknown")
        content = msg.get("content", "")
        timestamp = msg.get("timestamp", "")
        chat_name = msg.get("chat_name", "")
        if not content:
            continue
        if message_exists(sender, content, timestamp):
            continue
        msg_id = store_message(sender=sender, content=content, timestamp=timestamp,
                               is_outgoing=False, chat_name=chat_name)
        new_messages.append({"id": msg_id, "sender": sender, "content": content,
                              "chat_name": chat_name})

    if new_messages:
        threading.Thread(target=process_new_messages, args=(new_messages,), daemon=True).start()

    return jsonify({"ok": True, "stored": len(new_messages), "skipped": len(messages) - len(new_messages)})






@app.route("/api/test-message", methods=["POST"])
def api_test_message():
    """Simulate an incoming message for testing."""
    data = request.get_json()
    sender = data.get("sender", "Test Client")
    content = data.get("content", "")
    chat_name = data.get("chat_name", "Test Chat")

    if not content:
        return jsonify({"error": "content is required"}), 400

    msg_id = store_message(sender=sender, content=content, chat_name=chat_name)
    new_msg = [{"id": msg_id, "sender": sender, "content": content, "chat_name": chat_name}]
    process_new_messages(new_msg)

    return jsonify({"ok": True, "message_id": msg_id})


# ── Run ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=True)
