#!/usr/bin/env python3
"""
WhatsApp message scanner — monitors incoming messages on a WhatsApp number.

Adapted from whatsapp-analyzer's scraper.py. Instead of scraping group history,
this polls for new unread messages across all chats or a specific number.

Usage:
    python scanner.py                # continuous polling
    python scanner.py --once         # scan once and exit
    python scanner.py --login        # one-time QR login setup
"""

import json
import os
import sys
import time
import threading
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

SESSION_DIR = os.environ.get(
    "WHATSAPP_SESSION_DIR", str(Path.home() / ".whatsapp_session_cs")
)
SCAN_INTERVAL = int(os.environ.get("SCAN_INTERVAL", "30"))  # seconds
QR_PORT = int(os.environ.get("PORT", os.environ.get("QR_PORT", 8080)))

# Import db module for message storage
sys.path.insert(0, str(Path(__file__).parent))
from db import store_message, message_exists, init_db


# ── Logging ───────────────────────────────────────────────────────────────────

def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


# ── QR login server ───────────────────────────────────────────────────────────
# Serves screenshots of the WhatsApp QR code so you can scan remotely via browser.

_qr_screenshot: bytes = b""
_qr_lock = threading.Lock()

_QR_HTML = """<!DOCTYPE html><html><head><title>WhatsApp QR Login</title>
<meta http-equiv="refresh" content="4"></head>
<body style="background:#111;display:flex;flex-direction:column;align-items:center;
justify-content:center;height:100vh;margin:0;font-family:sans-serif;color:#fff;">
<h2>Scan with WhatsApp</h2>
<img src="/qr" style="max-width:350px;border-radius:8px;" onerror="this.alt='Waiting for QR...'">
<p style="color:#aaa;font-size:13px;">Page auto-refreshes every 4 seconds</p>
</body></html>"""


class _QRHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # suppress access logs

    def do_GET(self):
        if self.path == "/qr":
            with _qr_lock:
                data = _qr_screenshot
            if data:
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.end_headers()
                self.wfile.write(data)
            else:
                self.send_response(503)
                self.end_headers()
        else:
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(_QR_HTML.encode())


def _start_qr_server():
    server = HTTPServer(("0.0.0.0", QR_PORT), _QRHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    log(f"QR server running on port {QR_PORT} — open in browser to scan")
    return server


# ── Browser helpers ───────────────────────────────────────────────────────────

def _remove_browser_locks():
    """Delete stale Chromium profile lock files left by a crashed process."""
    session = Path(SESSION_DIR)
    lock_names = ("SingletonLock", "SingletonCookie", "SingletonSocket", "lockfile")
    for d in (session, session / "Default"):
        for name in lock_names:
            try:
                (d / name).unlink()
            except FileNotFoundError:
                pass
            except Exception:
                pass


_LOGIN_SELECTORS = [
    '[data-testid="chat-list-search"]',
    '[data-testid="search"]',
    '#side',
    'div[aria-label="Search or start new chat"]',
    '[data-testid="conversation-panel-wrapper"]',
]


def _wait_for_any(page, selectors: list, timeout_ms: int) -> bool:
    """Return True if any selector becomes visible within timeout_ms."""
    end = time.monotonic() + timeout_ms / 1000
    chunk = 2000
    while time.monotonic() < end:
        for sel in selectors:
            remaining = max(100, int((end - time.monotonic()) * 1000))
            try:
                page.wait_for_selector(sel, timeout=min(chunk, remaining))
                return True
            except PlaywrightTimeout:
                pass
            if time.monotonic() >= end:
                return False
    return False


def wait_for_login(page):
    """Wait for WhatsApp Web to be ready, serving QR screenshots if login needed."""
    log("Loading WhatsApp Web...")
    page.goto("https://web.whatsapp.com", wait_until="domcontentloaded")

    if _wait_for_any(page, _LOGIN_SELECTORS, timeout_ms=60000):
        log("[OK] Already logged in.")
        return

    log("=> QR scan required. Open the QR server URL in your browser to scan.")
    _start_qr_server()

    # Poll for login while pushing QR screenshots to the HTTP server
    end = time.monotonic() + 300
    while time.monotonic() < end:
        try:
            screenshot = page.screenshot()
            with _qr_lock:
                global _qr_screenshot
                _qr_screenshot = screenshot
        except Exception:
            pass

        for sel in _LOGIN_SELECTORS:
            try:
                page.wait_for_selector(sel, timeout=2000)
                log("[OK] Logged in successfully.")
                return
            except PlaywrightTimeout:
                pass

    raise PlaywrightTimeout("WhatsApp login timed out after 5 minutes.")


# ── Message scanning ──────────────────────────────────────────────────────────

def extract_unread_chats(page) -> list[dict]:
    """Find all chats with unread messages and extract the unread messages."""
    return page.evaluate(r"""
        () => {
            const results = [];

            // Find all chat list items with unread badges
            const chatItems = document.querySelectorAll('#pane-side [role="listitem"], #pane-side [data-testid="cell-frame-container"]');

            chatItems.forEach(item => {
                // Check for unread badge (green circle with number)
                const badge = item.querySelector('[data-testid="icon-unread-count"], span[aria-label*="unread"]');
                if (!badge) {
                    // Also check for any element that looks like an unread counter
                    const spans = item.querySelectorAll('span');
                    let hasUnread = false;
                    for (const span of spans) {
                        const text = span.textContent.trim();
                        if (/^\d+$/.test(text) && span.closest('[style*="background"]')) {
                            hasUnread = true;
                            break;
                        }
                    }
                    if (!hasUnread) return;
                }

                // Get chat name
                const titleEl = item.querySelector('[data-testid="cell-frame-title"] span, span[title][dir="auto"]');
                const chatName = titleEl ? (titleEl.getAttribute('title') || titleEl.textContent).trim() : 'Unknown';

                // Get last message preview
                const previewEl = item.querySelector('[data-testid="last-msg-status"] span, span[data-testid="msg-text"]');
                const preview = previewEl ? previewEl.textContent.trim() : '';

                // Get timestamp
                const timeEl = item.querySelector('[data-testid="cell-frame-secondary"] span, div[class] > span[dir="auto"]');
                const time = timeEl ? timeEl.textContent.trim() : '';

                results.push({
                    chatName: chatName,
                    preview: preview,
                    time: time,
                    element: null  // can't serialize DOM elements
                });
            });

            return results;
        }
    """)


def open_chat_and_extract(page, chat_name: str) -> list[dict]:
    """Open a specific chat and extract recent messages."""
    # Search for the chat
    search_selectors = [
        'div[contenteditable="true"][data-tab="3"]',
        'div[contenteditable="true"][title="Search or start new chat"]',
        '#side div[contenteditable="true"]',
        'p[contenteditable="true"]',
        '[data-testid="search-input"]',
    ]

    # Click search button first
    search_btn_selectors = [
        '[data-testid="chat-list-search"]',
        '[data-testid="search"]',
        'span[data-icon="search"]',
        '#side [aria-label*="Search"]',
    ]

    for sel in search_btn_selectors:
        try:
            el = page.locator(sel).first
            el.click(timeout=3000)
            break
        except Exception:
            pass

    page.wait_for_timeout(500)

    # Type chat name
    search_input = None
    for sel in search_selectors:
        try:
            el = page.locator(sel).first
            el.wait_for(state="visible", timeout=2000)
            search_input = el
            break
        except Exception:
            pass

    if not search_input:
        log(f"[!] Could not find search input for chat: {chat_name}")
        return []

    search_input.fill(chat_name)
    page.wait_for_timeout(1500)

    # Click the chat result
    escaped = chat_name.replace("\\", "\\\\").replace('"', '\\"')
    exact = page.locator(f'span[title="{escaped}"]').first
    if exact.count() > 0:
        exact.click()
    else:
        results = page.locator('[data-testid="cell-frame-title"]').filter(has_text=chat_name)
        if results.count() > 0:
            results.first.click()
        else:
            log(f"[!] Chat not found: {chat_name}")
            return []

    page.wait_for_timeout(2000)

    # Scroll to bottom to see latest messages
    page.evaluate("""
        const el = document.querySelector('#main div[role="application"]');
        if (el) el.scrollTop = el.scrollHeight;
    """)
    page.wait_for_timeout(1000)

    # Extract messages
    messages = page.evaluate(r"""
        () => {
            const results = [];
            const seen = new Set();

            function resolveRelativeDate(datePart) {
                const now = new Date();
                const fmt = d => `${d.getMonth()+1}/${d.getDate()}/${d.getFullYear()}`;
                if (!datePart) return fmt(now);
                const lower = datePart.trim().toLowerCase();
                if (lower === 'today') return fmt(now);
                if (lower === 'yesterday') {
                    const d = new Date(now); d.setDate(d.getDate() - 1); return fmt(d);
                }
                const days = ['sunday','monday','tuesday','wednesday','thursday','friday','saturday'];
                const dayIdx = days.indexOf(lower);
                if (dayIdx >= 0) {
                    const d = new Date(now);
                    const diff = (d.getDay() - dayIdx + 7) % 7 || 7;
                    d.setDate(d.getDate() - diff);
                    return fmt(d);
                }
                return null;
            }

            const bubbles = document.querySelectorAll('.copyable-text');
            bubbles.forEach(bubble => {
                try {
                    let row = null;
                    let el = bubble;
                    for (let i = 0; i < 10; i++) {
                        if (!el.parentElement) break;
                        el = el.parentElement;
                        const cls = el.className || '';
                        if (cls.includes('message-in') || cls.includes('message-out') ||
                            el.hasAttribute('data-id')) {
                            row = el;
                            break;
                        }
                    }
                    if (!row) row = bubble.parentElement?.parentElement || bubble;

                    const rowCls = row.className || '';
                    const rowHtml = row.outerHTML || '';
                    const isOutgoing = rowCls.includes('message-out') ||
                                       rowHtml.includes('message-out') ||
                                       !!row.querySelector('[data-testid="msg-tail-out"]');

                    let timestamp = null;
                    let sender = isOutgoing ? 'You' : 'Unknown';
                    const pre = bubble.getAttribute('data-pre-plain-text') || '';
                    const meta = pre.match(/\[([^\]]+)\]\s*(.*?):\s*$/);
                    if (meta) {
                        const rawTs = meta[1].trim();
                        sender = meta[2].trim();
                        const commaIdx = rawTs.indexOf(', ');
                        if (commaIdx >= 0) {
                            const timePart = rawTs.substring(0, commaIdx);
                            const datePart = rawTs.substring(commaIdx + 2);
                            const resolved = resolveRelativeDate(datePart);
                            timestamp = resolved ? `${timePart}, ${resolved}` : rawTs;
                        } else {
                            timestamp = `${rawTs}, ${resolveRelativeDate(null)}`;
                        }
                    }

                    const textEl = bubble.querySelector(
                        'span[data-testid="msg-text"] span[dir], ' +
                        'span.selectable-text span[dir], ' +
                        'span[dir]'
                    );
                    const text = textEl ? textEl.innerText.trim() : '';

                    if (!pre && !text) return;
                    if (!text) return;

                    const key = `${sender}|${timestamp}|${text}`;
                    if (seen.has(key)) return;
                    seen.add(key);

                    results.push({ sender, timestamp, text, isOutgoing });
                } catch (_) {}
            });

            return results;
        }
    """)

    # Clear search to go back to chat list
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(500)
        page.keyboard.press("Escape")
    except Exception:
        pass

    return messages


def scan_once(page) -> list[dict]:
    """Scan for new unread messages across all chats. Returns new messages found."""
    log("Scanning for unread messages...")

    unread_chats = extract_unread_chats(page)
    if not unread_chats:
        log("No unread messages found.")
        return []

    log(f"Found {len(unread_chats)} chat(s) with unread messages.")
    all_new = []

    for chat in unread_chats:
        chat_name = chat["chatName"]
        log(f"  Opening chat: {chat_name}")

        messages = open_chat_and_extract(page, chat_name)
        new_count = 0

        for msg in messages:
            if msg.get("isOutgoing"):
                continue  # Skip our own messages

            sender = msg.get("sender", "Unknown")
            content = msg.get("text", "")
            timestamp = msg.get("timestamp", "")

            if not content:
                continue

            # Dedup check
            if message_exists(sender, content, timestamp):
                continue

            msg_id = store_message(
                sender=sender,
                content=content,
                timestamp=timestamp,
                is_outgoing=False,
                chat_name=chat_name,
            )
            all_new.append({
                "id": msg_id,
                "sender": sender,
                "content": content,
                "timestamp": timestamp,
                "chat_name": chat_name,
            })
            new_count += 1

        if new_count:
            log(f"  [{chat_name}] {new_count} new message(s) stored.")

    log(f"Scan complete. {len(all_new)} new message(s) total.")
    return all_new


# ── Main entry points ─────────────────────────────────────────────────────────

def run_scanner(once: bool = False):
    """Main scanner loop — opens WhatsApp Web and polls for new messages."""
    init_db()
    _remove_browser_locks()

    with sync_playwright() as p:
        log(f"Launching browser (session: {SESSION_DIR})")
        context = p.chromium.launch_persistent_context(
            user_data_dir=SESSION_DIR,
            headless=False,
            viewport={"width": 1280, "height": 900},
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        )
        page = context.pages[0] if context.pages else context.new_page()
        wait_for_login(page)

        if once:
            new_messages = scan_once(page)
            if new_messages:
                from responder import process_new_messages
                process_new_messages(new_messages)
        else:
            log(f"Polling every {SCAN_INTERVAL}s. Press Ctrl+C to stop.")
            while True:
                try:
                    new_messages = scan_once(page)
                    if new_messages:
                        from responder import process_new_messages
                        process_new_messages(new_messages)
                except KeyboardInterrupt:
                    log("Stopped.")
                    break
                except Exception as e:
                    log(f"[!] Scan error: {e}")
                time.sleep(SCAN_INTERVAL)

        context.close()


def login_only():
    """One-time QR login setup — used locally, not on Railway."""
    log(f"Opening WhatsApp Web for login setup...")
    log(f"Session will be saved to: {SESSION_DIR}")
    _remove_browser_locks()
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=SESSION_DIR,
            headless=False,
            viewport={"width": 1280, "height": 900},
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        )
        page = context.pages[0] if context.pages else context.new_page()
        wait_for_login(page)
        log("[OK] Logged in! Session saved.")
        try:
            input("\nPress Enter to close the browser...")
        except EOFError:
            pass
        context.close()


if __name__ == "__main__":
    if "--login" in sys.argv:
        login_only()
    elif "--once" in sys.argv:
        run_scanner(once=True)
    else:
        run_scanner()
