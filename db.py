"""SQLite database for message queue and response tracking."""

import sqlite3
import json
import os
from datetime import datetime
from pathlib import Path

DB_PATH = os.environ.get("DB_PATH", str(Path(__file__).parent / "messages.db"))


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender TEXT NOT NULL,
            content TEXT NOT NULL,
            timestamp TEXT,
            received_at TEXT NOT NULL DEFAULT (datetime('now')),
            is_outgoing INTEGER NOT NULL DEFAULT 0,
            chat_name TEXT,
            status TEXT NOT NULL DEFAULT 'new'
            -- status: new, auto_replied, pending_review, replied, ignored
        );

        CREATE TABLE IF NOT EXISTS responses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id INTEGER NOT NULL REFERENCES messages(id),
            suggested_text TEXT NOT NULL,
            final_text TEXT,
            confidence REAL NOT NULL DEFAULT 0.0,
            auto_reply INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'pending',
            -- status: pending, approved, sent, rejected
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            sent_at TEXT
        );

        CREATE TABLE IF NOT EXISTS knowledge_docs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            source TEXT,
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_messages_status ON messages(status);
        CREATE INDEX IF NOT EXISTS idx_messages_received ON messages(received_at);
        CREATE INDEX IF NOT EXISTS idx_responses_status ON responses(status);
    """)
    conn.commit()
    conn.close()


def store_message(sender: str, content: str, timestamp: str = None,
                  is_outgoing: bool = False, chat_name: str = None) -> int:
    """Store an incoming message. Returns the message ID."""
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO messages (sender, content, timestamp, is_outgoing, chat_name) "
        "VALUES (?, ?, ?, ?, ?)",
        (sender, content, timestamp, int(is_outgoing), chat_name),
    )
    msg_id = cur.lastrowid
    conn.commit()
    conn.close()
    return msg_id


def store_response(message_id: int, suggested_text: str,
                   confidence: float, auto_reply: bool = False) -> int:
    """Store a suggested AI response for a message."""
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO responses (message_id, suggested_text, confidence, auto_reply) "
        "VALUES (?, ?, ?, ?)",
        (message_id, suggested_text, confidence, int(auto_reply)),
    )
    resp_id = cur.lastrowid

    # Update message status
    new_status = "auto_replied" if auto_reply else "pending_review"
    conn.execute("UPDATE messages SET status = ? WHERE id = ?", (new_status, message_id))
    conn.commit()
    conn.close()
    return resp_id


def get_pending_messages() -> list[dict]:
    """Get messages awaiting review (with their suggested responses)."""
    conn = get_db()
    rows = conn.execute("""
        SELECT m.id, m.sender, m.content, m.timestamp, m.received_at, m.chat_name, m.status,
               r.id as response_id, r.suggested_text, r.confidence, r.auto_reply, r.status as resp_status
        FROM messages m
        LEFT JOIN responses r ON r.message_id = m.id
        WHERE m.status IN ('new', 'pending_review')
        ORDER BY m.received_at DESC
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_auto_replied() -> list[dict]:
    """Get messages that were auto-replied."""
    conn = get_db()
    rows = conn.execute("""
        SELECT m.id, m.sender, m.content, m.timestamp, m.received_at, m.chat_name,
               r.suggested_text, r.created_at as replied_at
        FROM messages m
        JOIN responses r ON r.message_id = m.id
        WHERE m.status = 'auto_replied'
        ORDER BY m.received_at DESC
        LIMIT 50
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_all_messages(limit: int = 100) -> list[dict]:
    """Get recent messages with their responses."""
    conn = get_db()
    rows = conn.execute("""
        SELECT m.id, m.sender, m.content, m.timestamp, m.received_at, m.chat_name,
               m.status, m.is_outgoing,
               r.id as response_id, r.suggested_text, r.final_text,
               r.confidence, r.auto_reply, r.status as resp_status
        FROM messages m
        LEFT JOIN responses r ON r.message_id = m.id
        ORDER BY m.received_at DESC
        LIMIT ?
    """, (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def approve_response(response_id: int, final_text: str = None):
    """Approve a suggested response (optionally with edits)."""
    conn = get_db()
    if final_text:
        conn.execute(
            "UPDATE responses SET status = 'approved', final_text = ? WHERE id = ?",
            (final_text, response_id),
        )
    else:
        conn.execute(
            "UPDATE responses SET status = 'approved', final_text = suggested_text WHERE id = ?",
            (response_id,),
        )
    # Update the parent message status
    conn.execute("""
        UPDATE messages SET status = 'replied'
        WHERE id = (SELECT message_id FROM responses WHERE id = ?)
    """, (response_id,))
    conn.commit()
    conn.close()


def reject_response(response_id: int):
    """Reject a suggested response."""
    conn = get_db()
    conn.execute("UPDATE responses SET status = 'rejected' WHERE id = ?", (response_id,))
    conn.commit()
    conn.close()


def mark_sent(response_id: int):
    """Mark a response as sent."""
    conn = get_db()
    conn.execute(
        "UPDATE responses SET status = 'sent', sent_at = datetime('now') WHERE id = ?",
        (response_id,),
    )
    conn.commit()
    conn.close()


def message_exists(sender: str, content: str, timestamp: str) -> bool:
    """Check if a message already exists (dedup)."""
    conn = get_db()
    row = conn.execute(
        "SELECT 1 FROM messages WHERE sender = ? AND content = ? AND timestamp = ? LIMIT 1",
        (sender, content, timestamp),
    ).fetchone()
    conn.close()
    return row is not None


def load_knowledge_base() -> str:
    """Load all knowledge base documents as a single string for the AI prompt."""
    # First check the knowledge_base directory for markdown files
    kb_dir = Path(__file__).parent / "knowledge_base"
    parts = []
    if kb_dir.exists():
        for md_file in sorted(kb_dir.glob("*.md")):
            parts.append(md_file.read_text(encoding="utf-8"))

    # Also load any DB-stored documents
    conn = get_db()
    rows = conn.execute("SELECT title, content FROM knowledge_docs ORDER BY title").fetchall()
    conn.close()
    for row in rows:
        parts.append(f"## {row['title']}\n{row['content']}")

    return "\n\n---\n\n".join(parts)
