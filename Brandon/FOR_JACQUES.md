# WhatsApp Automation — What We're Building and Why

Hi Jacques,

After the call with Brendan, we've done a full review of what we've built so far and aligned on the right path forward. This note explains where we are, what the risks are, and what the new solution looks like.

---

## What We Built First (and Why We're Changing It)

The first version works by having a browser silently open WhatsApp Web in the background — it reads your messages, generates an AI-drafted reply, and shows it to your receptionist for review. It's essentially a robot pretending to be a person using WhatsApp on a computer.

**This works, but it has three serious problems:**

**1. Meta can ban your number.**
WhatsApp actively detects automated, non-human behaviour on WhatsApp Web. A bot clicks through chats in a systematic pattern that no human ever would. If Meta flags this, they can suspend or permanently ban your business number — the one all your patients use to reach you. Given that 100% of ExpandHealth's patient comms go through that number, losing it would be a serious operational problem.

**2. It checks for new messages only once per hour.**
Because the system has to open a real browser and scroll through chats, we deliberately limited how often it runs to avoid detection. This means a patient who sends a message could wait up to an hour before your receptionist even sees the AI-drafted reply. That's too slow for a clinical environment.

**3. It requires a persistent browser session that can break.**
If the WhatsApp session times out (which happens), the whole system stops working until someone re-scans the QR code. It's fragile infrastructure for something your practice depends on daily.

---

## What We're Building Instead

We're migrating to the **official WhatsApp Business API** — the same technology Brendan's platform uses. This is Meta's own product for businesses, and it's free to receive and reply to messages.

Here's how it works:

- Your existing clinic number stays the same
- When a patient messages you, WhatsApp instantly notifies our system (no polling, no browser)
- The AI generates a draft reply within seconds
- Your receptionist sees it in the dashboard, edits if needed, and clicks Send
- The message goes out via the official API — no browser automation, no scraping, no Terms of Service risk

**The experience for your receptionist is identical.** Same dashboard, same review-before-send flow, same knowledge base. The difference is entirely behind the scenes — it's now built on infrastructure Meta explicitly supports and encourages businesses to use.

---

## What About Sending Follow-Up Messages to Patients?

This was the key concern you raised — e.g., after a consultation, sending a personalised next-steps message that afternoon. The API has a rule: you can only send the first message using a pre-approved template. But here's how we handle it cleanly:

1. Your receptionist sends a short template message after the visit:
   *"Hi [Name], thank you for visiting ExpandHealth today. We've prepared your personalised next steps — reply to this message to receive them."*
2. The patient replies (even just "ok" or a thumbs up)
3. That reply opens a 24-hour window where your team can send any message — fully personalised, no template restrictions

This adds one small step but keeps everything compliant. Templates cost around R2–3 per send, which is negligible.

---

## The Migration

Here's what the changeover looks like practically:

**Before we switch:**
- Export all contacts and message history from the current WhatsApp Business app (takes about 10 minutes)

**The switch:**
- Register your existing number on the WhatsApp Business API through Meta's free platform
- Your number stays the same; your patients don't notice anything
- You lose the WhatsApp app on the phone for this number — all messaging happens through the dashboard instead

**After the switch:**
- Messages arrive in real time (not hourly)
- No more risk of the number being blocked
- The AI draft appears in the dashboard within seconds of a patient sending a message
- The send button in the dashboard sends via the official API

---

## Summary

| | Current (Playwright) | New (Official API) |
|---|---|---|
| Response time | Up to 1 hour | Seconds |
| Ban risk | Real and documented | None |
| Reliability | Session can break | Always on |
| Outbound follow-up | Unlimited (but illegal) | Template to unlock, then free |
| Cost | Free | ~R2–3 per outbound template |

The new system is faster, safer, and more reliable. It's also the foundation Brendan's platform is built on — we're building the same thing, purpose-built for ExpandHealth.

Let us know if you have any questions before we proceed.

— Emilian
