#!/usr/bin/env python3
"""
AI Response Generator — uses Claude to generate suggested replies to WhatsApp messages.

Classifies messages into:
- AUTO_REPLY: Safe to answer automatically (greetings, hours, booking confirmations, pricing)
- NEEDS_REVIEW: Complex or sensitive — put in human review queue

Uses the clinic knowledge base for context.
"""

import os
import json
import sys
from pathlib import Path
from datetime import datetime

from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, str(Path(__file__).parent))
from db import store_response, load_knowledge_base, get_db

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
AUTO_REPLY_THRESHOLD = float(os.environ.get("AUTO_REPLY_THRESHOLD", "0.85"))


def get_client():
    """Lazy-load the Anthropic client."""
    import anthropic
    return anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


SYSTEM_PROMPT_TEMPLATE = """You are a helpful, professional receptionist AI for ExpandHealth, \
a longevity and performance health clinic in Cape Town, South Africa.

Your job is to draft WhatsApp reply messages for incoming client inquiries.
Be warm, professional, and concise — these are WhatsApp messages, keep them short.

## Clinic Knowledge Base
{knowledge_base}

## Response Rules
1. Always be polite and welcoming
2. For booking requests: confirm interest, say the team will follow up shortly
3. For pricing questions: share pricing if available in the knowledge base, otherwise say "I'll check with the team and get back to you"
4. For medical questions: DO NOT give medical advice. Say "That's a great question for our practitioners — let me book you a consultation"
5. For opening hours / location: share directly from the knowledge base
6. For complaints: acknowledge, empathize, escalate to management
7. Sign off as "ExpandHealth Team" or similar — never pretend to be a doctor
8. Use WhatsApp-friendly formatting: *bold* for emphasis, keep paragraphs short

## Output Format
Respond with a JSON object (no markdown code fence):
{{
    "reply": "The suggested WhatsApp reply text",
    "confidence": 0.0-1.0,
    "category": "greeting|booking|pricing|hours|medical|complaint|general|unknown",
    "auto_safe": true/false,
    "reasoning": "Brief explanation of why this response was chosen"
}}

- confidence: How confident you are this is the right response (0.0-1.0)
- auto_safe: true if this can be sent without human review (simple/factual queries)
- auto_safe should be FALSE for: medical questions, complaints, complex multi-part questions, \
anything ambiguous, anything that could have legal implications
"""


def generate_response(sender: str, content: str, chat_name: str = None,
                      recent_context: list = None) -> dict:
    """Generate an AI response for a message.

    Returns dict with: reply, confidence, category, auto_safe, reasoning
    """
    if not ANTHROPIC_API_KEY:
        return {
            "reply": "[AI responder not configured — set ANTHROPIC_API_KEY]",
            "confidence": 0.0,
            "category": "unknown",
            "auto_safe": False,
            "reasoning": "No API key configured",
        }

    knowledge_base = load_knowledge_base()
    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(knowledge_base=knowledge_base)

    # Build context from recent messages if available
    context_str = ""
    if recent_context:
        context_lines = []
        for msg in recent_context[-5:]:  # last 5 messages for context
            direction = "Client" if not msg.get("is_outgoing") else "Us"
            context_lines.append(f"{direction}: {msg.get('content', '')}")
        context_str = "\n\nRecent conversation:\n" + "\n".join(context_lines) + "\n"

    user_message = f"New message from {sender}"
    if chat_name:
        user_message += f" (chat: {chat_name})"
    user_message += f":\n\n{content}"
    if context_str:
        user_message += context_str

    client = get_client()
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
    )

    # Parse the JSON response
    raw_text = response.content[0].text.strip()
    # Handle potential markdown code fences
    if raw_text.startswith("```"):
        raw_text = raw_text.split("\n", 1)[1].rsplit("```", 1)[0].strip()

    try:
        result = json.loads(raw_text)
    except json.JSONDecodeError:
        result = {
            "reply": raw_text,
            "confidence": 0.5,
            "category": "general",
            "auto_safe": False,
            "reasoning": "Could not parse structured response",
        }

    return result


def process_new_messages(messages: list[dict]):
    """Process a batch of new messages — generate AI responses and store them."""
    log(f"Processing {len(messages)} new message(s) with AI...")

    for msg in messages:
        msg_id = msg["id"]
        sender = msg["sender"]
        content = msg["content"]
        chat_name = msg.get("chat_name")

        try:
            result = generate_response(sender, content, chat_name)

            confidence = result.get("confidence", 0.0)
            auto_safe = result.get("auto_safe", False)
            reply_text = result.get("reply", "")

            # Only auto-reply if confidence is above threshold AND marked as safe
            auto_reply = auto_safe and confidence >= AUTO_REPLY_THRESHOLD

            store_response(
                message_id=msg_id,
                suggested_text=reply_text,
                confidence=confidence,
                auto_reply=auto_reply,
            )

            status = "AUTO-REPLY" if auto_reply else "NEEDS REVIEW"
            category = result.get("category", "unknown")
            log(f"  [{status}] {sender}: {content[:50]}... → {category} ({confidence:.0%})")

        except Exception as e:
            log(f"  [!] Failed to generate response for msg #{msg_id}: {e}")
            store_response(
                message_id=msg_id,
                suggested_text=f"[Error generating response: {e}]",
                confidence=0.0,
                auto_reply=False,
            )


def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


if __name__ == "__main__":
    # Test mode: generate a response for a sample message
    print("Testing AI responder...")
    result = generate_response(
        sender="Test Client",
        content="Hi, I'd like to book a red light therapy session. What are your prices?",
        chat_name="Test Chat",
    )
    print(json.dumps(result, indent=2))
