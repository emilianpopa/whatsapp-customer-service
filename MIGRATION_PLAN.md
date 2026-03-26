# Migration Plan: Playwright Scraping → WhatsApp Business Cloud API

## Overview

Replace `scanner.py` (Playwright/WhatsApp Web scraping) with the official Meta WhatsApp Business Cloud API.
Everything else — `responder.py`, `web/app.py`, `db.py`, knowledge base — stays the same or changes minimally.

---

## Current Architecture Weaknesses

| Problem | Root Cause | Impact |
|---|---|---|
| ToS violation / ban risk | WhatsApp Web scraping on a server is explicitly prohibited by Meta | Number ban = total loss of patient comms |
| 1-hour message delay | Playwright scan interval kept high to reduce detection risk | Unacceptable for clinical response times |
| Fragile session management | Persistent Chromium profile on Railway volume; QR re-scan needed on expiry | System silently breaks; requires manual intervention |
| Heavy infrastructure | Full Chromium browser running 24/7 on Railway | Memory/CPU overhead; crash risk |
| Headless browser quirks | `headless=False` requires Xvfb; DOM selectors break on WhatsApp Web updates | Maintenance burden every time WhatsApp updates their UI |
| Polling send flow | Scanner polls `/api/approved-responses` hourly, then types into WhatsApp Web | Sends can be delayed up to 1hr after approval |

---

## Target Architecture

```
Patient → WhatsApp → Meta Cloud API → Webhook (Flask) → db.py → responder.py
                                                                       ↓
Receptionist ← Dashboard (Flask) ← DB ← AI draft
                    ↓ (approve + click Send)
            Meta Cloud API → WhatsApp → Patient
```

- No browser. No polling. No session management.
- Messages arrive via webhook in real time.
- Sends happen via a single HTTP call to Meta's API.

---

## Phase 1: Meta Setup (Manual — Prerequisites)

These steps are done once by Emilian/Jacques before any code changes.

1. **Create Meta Business Account** at business.facebook.com (if not already done)
2. **Create a Meta App** → type: Business → add WhatsApp product
3. **Register the ExpandHealth number** on the WhatsApp Business API
   - This migrates the existing number to API mode
   - **Before doing this:** export contacts + chat history from the WhatsApp Business app
   - After migration: the phone app no longer works for this number
4. **Note down these values** (needed as env vars):
   - `WHATSAPP_PHONE_NUMBER_ID` — shown in Meta App dashboard
   - `WHATSAPP_BUSINESS_ACCOUNT_ID` — shown in Meta App dashboard
   - `WHATSAPP_ACCESS_TOKEN` — generate a permanent token via System User
   - `WEBHOOK_VERIFY_TOKEN` — any random string you choose (e.g. `expandhealth-webhook-2026`)
5. **Set up a message template** for proactive outbound:
   - Template name: `post_visit_followup`
   - Body: "Hi {{1}}, thank you for visiting ExpandHealth today. We've prepared your personalised next steps — reply to this message to receive them."
   - Category: UTILITY
   - Submit for Meta approval (usually <24hr)

---

## Phase 2: New Files to Create

### `whatsapp_api.py` — API Client (replaces Playwright send logic)

```python
import os, requests

GRAPH_API = "https://graph.facebook.com/v21.0"
PHONE_NUMBER_ID = os.environ["WHATSAPP_PHONE_NUMBER_ID"]
ACCESS_TOKEN = os.environ["WHATSAPP_ACCESS_TOKEN"]

def send_message(to: str, text: str) -> dict:
    """Send a free-form text message (within 24hr window)."""
    url = f"{GRAPH_API}/{PHONE_NUMBER_ID}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text}
    }
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}", "Content-Type": "application/json"}
    r = requests.post(url, json=payload, headers=headers)
    r.raise_for_status()
    return r.json()

def send_template(to: str, patient_name: str) -> dict:
    """Send the post-visit follow-up template to open a conversation window."""
    url = f"{GRAPH_API}/{PHONE_NUMBER_ID}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {
            "name": "post_visit_followup",
            "language": {"code": "en"},
            "components": [{
                "type": "body",
                "parameters": [{"type": "text", "text": patient_name}]
            }]
        }
    }
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}", "Content-Type": "application/json"}
    r = requests.post(url, json=payload, headers=headers)
    r.raise_for_status()
    return r.json()
```

---

## Phase 3: Changes to `web/app.py`

### 3a. Add webhook verification endpoint

Meta calls this during setup to confirm the webhook URL is valid.

```python
@app.route('/webhook', methods=['GET'])
def webhook_verify():
    mode = request.args.get('hub.mode')
    token = request.args.get('hub.verify_token')
    challenge = request.args.get('hub.challenge')
    if mode == 'subscribe' and token == os.environ['WEBHOOK_VERIFY_TOKEN']:
        return challenge, 200
    return 'Forbidden', 403
```

### 3b. Add webhook message receiver

This replaces the `/api/ingest` flow that scanner.py used.

```python
@app.route('/webhook', methods=['POST'])
def webhook_receive():
    data = request.get_json()
    try:
        entry = data['entry'][0]['changes'][0]['value']
        # Ignore status updates (delivered, read receipts)
        if 'messages' not in entry:
            return 'ok', 200
        for msg in entry['messages']:
            if msg['type'] != 'text':
                continue  # ignore images/audio for now
            sender = msg['from']        # e.g. "27821234567"
            content = msg['text']['body']
            wa_id = msg['id']
            timestamp = msg['timestamp']  # Unix epoch
            contact = entry.get('contacts', [{}])[0]
            chat_name = contact.get('profile', {}).get('name', sender)

            if not db.message_exists_by_wa_id(wa_id):
                db.store_message(sender, content, timestamp, chat_name, wa_message_id=wa_id)
                # Trigger AI response generation
                threading.Thread(
                    target=responder.process_new_messages
                ).start()
    except (KeyError, IndexError):
        pass  # malformed payload — ignore
    return 'ok', 200
```

### 3c. Update `/api/approve/<id>` to send via API

Replace the current "mark as approved, scanner will send it later" pattern:

```python
@app.route('/api/approve/<int:msg_id>', methods=['POST'])
def approve_response(msg_id):
    data = request.get_json() or {}
    final_text = data.get('text', '').strip()
    # ... existing validation logic ...

    # Send immediately via API (no more scanner polling)
    from whatsapp_api import send_message
    message = db.get_message(msg_id)
    send_message(to=message['sender'], text=final_text)

    db.mark_response_sent(msg_id, final_text)
    return jsonify({'status': 'sent'})
```

### 3d. Remove

- `/api/approved-responses` route (no longer needed — scanner no longer polls)
- `/api/sent/<id>` route (no longer needed — send is synchronous now)

---

## Phase 4: Changes to `db.py`

Add `wa_message_id` column to `messages` table for deduplication (Meta may deliver the same webhook twice).

```python
# Add to CREATE TABLE messages:
wa_message_id TEXT UNIQUE

# Add helper:
def message_exists_by_wa_id(wa_id: str) -> bool:
    conn = get_db()
    row = conn.execute(
        "SELECT id FROM messages WHERE wa_message_id = ?", (wa_id,)
    ).fetchone()
    conn.close()
    return row is not None
```

---

## Phase 5: Retire `scanner.py`

- Remove from Railway as a separate service
- Remove `playwright` from `requirements.txt`
- Remove persistent volume (WhatsApp session no longer needed)
- Remove `SCAN_INTERVAL`, `WHATSAPP_SESSION_DIR`, `WEB_SERVICE_URL`, `MAX_CHATS`, `SKIP_GROUPS`, `EXCLUDED_CHATS`, `WHITELIST_CHATS` env vars

---

## Phase 6: New Environment Variables

Add to Railway (web service only):

```
WHATSAPP_PHONE_NUMBER_ID=<from Meta App dashboard>
WHATSAPP_BUSINESS_ACCOUNT_ID=<from Meta App dashboard>
WHATSAPP_ACCESS_TOKEN=<permanent system user token>
WEBHOOK_VERIFY_TOKEN=expandhealth-webhook-2026
```

---

## Phase 7: Update Webhook URL in Meta Dashboard

Set the webhook URL to: `https://<your-railway-domain>/webhook`

Subscribe to: `messages`

---

## What Doesn't Change

- `responder.py` — zero changes
- `web/app.py` dashboard UI — zero changes
- `db.py` core schema — minor addition only
- `knowledge_base/` — zero changes
- The receptionist workflow — identical

---

## Rollout Order

1. Phase 1 — Meta setup (manual, ~1hr)
2. Phase 2 — Write `whatsapp_api.py`
3. Phase 3 — Webhook endpoints + approve flow in `web/app.py`
4. Phase 4 — `db.py` `wa_message_id` column
5. **Test on a staging number** before switching the real ExpandHealth number
6. Phase 5 — Once tested, retire scanner.py + Playwright
7. Phase 6 + 7 — Swap env vars, point webhook URL

---

## Estimated Effort

| Phase | Effort |
|---|---|
| Meta setup (manual) | ~1hr |
| `whatsapp_api.py` | ~30min |
| `web/app.py` changes | ~2hr |
| `db.py` changes | ~30min |
| Testing | ~1hr |
| Total | ~5hr coding + Meta setup |
