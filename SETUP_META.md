# Meta WhatsApp Business Cloud API — Manual Setup

Do these steps once before deploying. Takes about 1 hour.

---

## Phase 1 — Meta Setup

### Step 1: Export current WhatsApp data (do this first, before anything else)

On the ExpandHealth phone:
1. Open WhatsApp Business → Settings → Chats → Chat Backup → Back Up Now
2. Settings → Chats → Export Chat (repeat for any key conversations worth keeping as reference)
3. Download the full contacts list: Settings → Business Tools → Facebook Pages → (or simply screenshot the contact list)

**Why first:** once you migrate the number to the API, the phone app stops working for that number. You cannot undo this without losing API access.

---

### Step 2: Create a Meta Business Account

1. Go to https://business.facebook.com
2. Sign in with a Facebook account (Emilian's or a dedicated ExpandHealth account)
3. Click **Create Account** → fill in business name "ExpandHealth"
4. Verify the business (may require ID or business docs)

---

### Step 3: Create a Meta Developer App

1. Go to https://developers.facebook.com
2. Click **My Apps** → **Create App**
3. Select **Business** as the app type
4. App name: `ExpandHealth WhatsApp Bot` (internal only, patients never see this)
5. Connect it to your Meta Business Account
6. In the app dashboard, click **Add Product** → find **WhatsApp** → click **Set Up**

---

### Step 4: Add a phone number

1. In the WhatsApp product section, go to **API Setup**
2. You'll see a **Step 1: Add a phone number** section
3. Click **Add Phone Number**
4. Enter the ExpandHealth WhatsApp Business number
5. Verify via SMS or voice call
6. **Note down the `Phone Number ID`** — shown on the API Setup page (looks like `123456789012345`)
7. **Note down the `WhatsApp Business Account ID`** — shown just above it

---

### Step 5: Generate a permanent access token

The default test token expires in 24 hours. You need a permanent one.

1. Go to **Business Settings** → **Users** → **System Users**
2. Click **Add** → name it `expandhealth-bot` → role: **Admin**
3. Click **Generate New Token**
4. Select your app → tick `whatsapp_business_messaging` and `whatsapp_business_management`
5. Click **Generate Token** → **copy it immediately** (it's shown only once)
6. This is your `WHATSAPP_ACCESS_TOKEN`

---

### Step 6: Submit the post-visit follow-up template

This is needed for proactive outbound (e.g., sending a message to a patient first).

1. In the WhatsApp product section → **Message Templates** → **Create Template**
2. Fill in:
   - **Category:** Utility
   - **Name:** `post_visit_followup` (must match exactly, lowercase, underscores)
   - **Language:** English
   - **Body text:**
     ```
     Hi {{1}}, thank you for visiting ExpandHealth today. We've prepared your personalised next steps — reply to this message to receive them.
     ```
3. Submit for review — Meta usually approves Utility templates within a few hours

---

## Phase 6 — Set Environment Variables on Railway

In your Railway project → **web service** → **Variables**, add:

| Variable | Value |
|---|---|
| `WHATSAPP_PHONE_NUMBER_ID` | The Phone Number ID from Step 4 |
| `WHATSAPP_ACCESS_TOKEN` | The permanent token from Step 5 |
| `WEBHOOK_VERIFY_TOKEN` | Choose any random string, e.g. `expandhealth-2026-secure` |

Remove (or leave — they're just ignored now):
- `SCAN_INTERVAL`
- `WHATSAPP_SESSION_DIR`
- `WEB_SERVICE_URL`
- `MAX_CHATS`
- `SKIP_GROUPS`

---

## Phase 7 — Register the Webhook URL with Meta

Do this after Railway has deployed the new code (after Phase 6 env vars are saved).

1. In your Meta app → WhatsApp → **Configuration**
2. Under **Webhook**, click **Edit**
3. Set:
   - **Callback URL:** `https://<your-railway-domain>/webhook`
     (find your Railway domain in the Railway dashboard → web service → Settings → Domains)
   - **Verify Token:** the same string you set as `WEBHOOK_VERIFY_TOKEN` above
4. Click **Verify and Save** — Meta will call `GET /webhook` to confirm; if it returns 200, you're done
5. Under **Webhook Fields**, click **Manage** → tick **messages** → Save

---

## Testing

Once the webhook is registered:

1. Send a WhatsApp message **to** the ExpandHealth number from any phone
2. It should appear in the dashboard within a few seconds
3. Approve the AI draft — it should send instantly (no scanner needed)

To test the template send, use the `/api/send-template` endpoint (see below) or test from the Meta dashboard's **API Explorer**.

---

## Rollback

If anything goes wrong:
- The old `scanner.py` still exists and works — just set the old env vars back and run it locally
- The API migration can be paused at any point before Step 4 (phone number registration) with zero impact
- After Step 4, reverting requires contacting Meta support to de-register the number from the API
