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
# When running as a separate Railway service, post messages to the web service API.
# Set to empty string or omit to write directly to SQLite (local dev mode).
WEB_SERVICE_URL = os.environ.get("WEB_SERVICE_URL", "").rstrip("/")

if not WEB_SERVICE_URL:
    # Local dev: write directly to SQLite
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
<img src="/qr" style="max-width:350px;border-radius:8px;" onerror="this.style.display='none'">
<p id="status" style="color:#aaa;font-size:13px;">Page auto-refreshes every 4 seconds</p>
</body></html>"""

_LOADING_HTML = """<!DOCTYPE html><html><head><title>WhatsApp Scanner</title>
<meta http-equiv="refresh" content="5"></head>
<body style="background:#111;display:flex;flex-direction:column;align-items:center;
justify-content:center;height:100vh;margin:0;font-family:sans-serif;color:#fff;">
<h2>Scanner starting...</h2>
<p style="color:#aaa;font-size:13px;">Loading WhatsApp Web. Page auto-refreshes every 5 seconds.</p>
</body></html>"""

_qr_needed = False  # set to True once QR login is required


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
            html = _QR_HTML if _qr_needed else _LOADING_HTML
            self.wfile.write(html.encode())


_qr_server_started = False

def _start_qr_server():
    global _qr_server_started
    if _qr_server_started:
        return
    server = HTTPServer(("0.0.0.0", QR_PORT), _QRHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    _qr_server_started = True
    log(f"HTTP server running on port {QR_PORT}")


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

    global _qr_needed
    _qr_needed = True
    log("=> QR scan required. Open the QR server URL in your browser to scan.")

    # Poll indefinitely — keep serving screenshots until the user scans
    while True:
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
                _qr_needed = False
                return
            except PlaywrightTimeout:
                pass


# ── Message scanning ──────────────────────────────────────────────────────────

MAX_CHATS = int(os.environ.get("MAX_CHATS", "30"))  # how many sidebar chats to monitor
SKIP_GROUPS = os.environ.get("SKIP_GROUPS", "true").lower() in ("1", "true", "yes")
# Comma-separated list of chat names to always skip (e.g. group chats)
EXCLUDED_CHATS = {
    name.strip()
    for name in os.environ.get("EXCLUDED_CHATS", "").split(",")
    if name.strip()
}

# In-memory state: tracks last-seen preview+time per chat to detect new activity
_chat_last_seen: dict = {}  # {chat_name: (preview, time)}


def get_sidebar_chats(page) -> list[dict]:
    """Get all visible chats from the sidebar (name, last message preview, timestamp).

    Uses span[title] anchors since WhatsApp Web no longer uses role=listitem
    or data-testid=cell-frame-container in newer versions.
    """
    return page.evaluate(r"""
        () => {
            const results = [];
            const seen = new Set();

            // Find all title spans in the sidebar — these are chat/contact names.
            // Filter out message preview spans (they contain newlines or RTL/LTR embedding chars).
            const titleSpans = document.querySelectorAll('#pane-side span[title]');

            titleSpans.forEach(titleSpan => {
                const chatName = (titleSpan.getAttribute('title') || titleSpan.textContent).trim();
                // Skip empty, already-seen, long strings (previews), or strings with newlines/embedding chars
                if (!chatName || seen.has(chatName)) return;
                if (chatName.length > 80) return;
                if (chatName.includes('\n') || chatName.includes('\u202a') || chatName.includes('\u202c')) return;
                seen.add(chatName);

                // Walk up to find the chat row container (up to 8 levels)
                let container = titleSpan.parentElement;
                for (let i = 0; i < 8; i++) {
                    if (!container) break;
                    const role = container.getAttribute('role');
                    const tabindex = container.getAttribute('tabindex');
                    if (role === 'button' || role === 'listitem' || tabindex === '0') break;
                    container = container.parentElement;
                }
                if (!container) return;

                // Grab all text spans to find preview + time
                const allSpans = Array.from(container.querySelectorAll('span'));
                const texts = allSpans
                    .map(s => s.textContent.trim())
                    .filter(t => t.length > 0 && t !== chatName);

                // Timestamp is usually short (e.g. "Yesterday", "10:30") — heuristic: last short text
                const timeGuess = texts.filter(t => t.length < 20).pop() || '';
                const preview = texts.filter(t => t !== timeGuess && t !== '(You)').join(' ').slice(0, 100);

                // Detect group chats using WhatsApp JID: group IDs end in @g.us, DMs end in @c.us.
                // Walk up to find any element with data-id containing the JID.
                let isGroup = false;
                let el2 = container;
                for (let i = 0; i < 12; i++) {
                    if (!el2) break;
                    const dataId = el2.getAttribute('data-id') || el2.getAttribute('data-jid') || '';
                    if (dataId.includes('@g.us')) { isGroup = true; break; }
                    if (dataId.includes('@c.us') || dataId.includes('@s.whatsapp.net')) { isGroup = false; break; }
                    el2 = el2.parentElement;
                }
                // Fallback: check for group icon if JID not found
                if (!isGroup) {
                    const containerHtml = container.innerHTML || '';
                    isGroup = containerHtml.includes('default-group') || containerHtml.includes('group-photo');
                }

                results.push({ chatName, preview, time: timeGuess, isGroup });
            });

            return results;
        }
    """)


def open_chat_and_extract(page, chat_name: str) -> list[dict]:
    """Open a specific chat by clicking its title span in the sidebar, then extract messages."""
    # Click directly on the span[title] in the sidebar — no search needed
    escaped = chat_name.replace("\\", "\\\\").replace('"', '\\"')
    title_span = page.locator(f'#pane-side span[title="{escaped}"]').first
    try:
        title_span.wait_for(state="visible", timeout=3000)
        title_span.click()
    except Exception:
        # Fallback: press Escape to close any open panel, then retry
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(500)
            title_span.click(timeout=3000)
        except Exception:
            log(f"[!] Could not open chat: {chat_name}")
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

    # Press Escape to close the chat and return to the chat list
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)
    except Exception:
        pass

    return messages


def scan_once(page) -> list[dict]:
    """Scan recent chats for new messages using sidebar state diffing + DB dedup."""
    log("Scanning chats...")

    sidebar = get_sidebar_chats(page)[:MAX_CHATS]
    if not sidebar:
        log("No chats found in sidebar.")
        return []

    # Skip explicitly excluded chats first
    if EXCLUDED_CHATS:
        excluded = [c["chatName"] for c in sidebar if c["chatName"] in EXCLUDED_CHATS]
        if excluded:
            log(f"Skipping excluded chat(s): {excluded}")
        sidebar = [c for c in sidebar if c["chatName"] not in EXCLUDED_CHATS]

    # Log all detected chats for debugging
    for c in sidebar:
        log(f"  Chat: {c['chatName']!r} isGroup={c.get('isGroup', False)}")

    # Optionally skip group chats
    if SKIP_GROUPS:
        groups = [c["chatName"] for c in sidebar if c.get("isGroup")]
        if groups:
            log(f"Skipping {len(groups)} group(s): {groups}")
        sidebar = [c for c in sidebar if not c.get("isGroup")]

    # Find chats with changed last-message preview/time
    chats_to_open = []
    for chat in sidebar:
        name = chat["chatName"]
        key = (chat["preview"], chat["time"])
        if _chat_last_seen.get(name) != key:
            chats_to_open.append(chat)

    # On very first scan, just record current state — don't open every chat
    if not _chat_last_seen:
        for chat in sidebar:
            _chat_last_seen[chat["chatName"]] = (chat["preview"], chat["time"])
        log(f"Initialised state for {len(sidebar)} chat(s). Will detect changes from next scan.")
        return []

    if not chats_to_open:
        log(f"No new activity across {len(sidebar)} chat(s).")
        return []

    log(f"Activity in {len(chats_to_open)} chat(s): {[c['chatName'] for c in chats_to_open]}")

    all_candidates = []
    for chat in chats_to_open:
        chat_name = chat["chatName"]
        messages = open_chat_and_extract(page, chat_name)
        for msg in messages:
            if msg.get("isOutgoing"):
                continue
            content = msg.get("text", "")
            if not content:
                continue
            all_candidates.append({
                "sender": msg.get("sender", "Unknown"),
                "content": content,
                "timestamp": msg.get("timestamp", ""),
                "chat_name": chat_name,
            })
        # Update last-seen state after opening
        _chat_last_seen[chat_name] = (chat["preview"], chat["time"])

    # Also update state for chats we didn't open (no change)
    for chat in sidebar:
        name = chat["chatName"]
        if name not in _chat_last_seen:
            _chat_last_seen[name] = (chat["preview"], chat["time"])

    if not all_candidates:
        log("No new incoming messages found.")
        return []

    if WEB_SERVICE_URL:
        import urllib.request
        payload = json.dumps({"messages": all_candidates}).encode()
        req = urllib.request.Request(
            f"{WEB_SERVICE_URL}/api/ingest",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
        stored = result.get("stored", 0)
        log(f"Scan complete. {stored} new message(s) ingested via web API.")
        return []
    else:
        all_new = []
        for msg in all_candidates:
            if message_exists(msg["sender"], msg["content"], msg["timestamp"]):
                continue
            msg_id = store_message(
                sender=msg["sender"],
                content=msg["content"],
                timestamp=msg["timestamp"],
                is_outgoing=False,
                chat_name=msg["chat_name"],
            )
            all_new.append({"id": msg_id, **msg})
        log(f"Scan complete. {len(all_new)} new message(s) total.")
        return all_new


# ── Main entry points ─────────────────────────────────────────────────────────

def run_scanner(once: bool = False):
    """Main scanner loop — opens WhatsApp Web and polls for new messages."""
    if not WEB_SERVICE_URL:
        init_db()
    _start_qr_server()  # start HTTP server immediately so URL always responds
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
            if new_messages and not WEB_SERVICE_URL:
                from responder import process_new_messages
                process_new_messages(new_messages)
        else:
            log(f"Polling every {SCAN_INTERVAL}s. Press Ctrl+C to stop.")
            while True:
                try:
                    new_messages = scan_once(page)
                    if new_messages and not WEB_SERVICE_URL:
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
