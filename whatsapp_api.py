"""
WhatsApp Business Cloud API client.

Handles sending messages via Meta's official API.
Replaces the Playwright-based send flow in scanner.py.

Environment variables required:
    WHATSAPP_PHONE_NUMBER_ID  — from Meta App dashboard
    WHATSAPP_ACCESS_TOKEN     — permanent system user token
    WHATSAPP_POST_VISIT_TEMPLATE — template name for outbound (default: post_visit_followup)
"""

import os
import logging
import requests

logger = logging.getLogger(__name__)

GRAPH_API = "https://graph.facebook.com/v21.0"


def _phone_number_id() -> str:
    val = os.environ.get("WHATSAPP_PHONE_NUMBER_ID", "")
    if not val:
        raise RuntimeError("WHATSAPP_PHONE_NUMBER_ID env var is not set")
    return val


def _access_token() -> str:
    val = os.environ.get("WHATSAPP_ACCESS_TOKEN", "")
    if not val:
        raise RuntimeError("WHATSAPP_ACCESS_TOKEN env var is not set")
    return val


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {_access_token()}",
        "Content-Type": "application/json",
    }


def send_message(to: str, text: str) -> dict:
    """Send a free-form text reply within the 24-hour conversation window.

    Args:
        to:   Recipient phone number in E.164 format, no '+' (e.g. '27821234567').
        text: Message body to send.

    Returns:
        Meta API response dict on success.

    Raises:
        requests.HTTPError on API failure.
    """
    url = f"{GRAPH_API}/{_phone_number_id()}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text, "preview_url": False},
    }
    r = requests.post(url, json=payload, headers=_headers(), timeout=15)
    if not r.ok:
        logger.error("WhatsApp send failed: %s — %s", r.status_code, r.text)
    r.raise_for_status()
    logger.info("Message sent to %s", to)
    return r.json()


def send_template(to: str, patient_name: str, template_name: str = None) -> dict:
    """Send a pre-approved outbound template to open a conversation window.

    Use this when more than 24 hours have passed since the patient last messaged.
    Once the patient replies, use send_message() for free-form responses.

    Args:
        to:            Recipient phone number in E.164 format, no '+'.
        patient_name:  Patient's first name, used as the {{1}} template variable.
        template_name: Override the template name (defaults to WHATSAPP_POST_VISIT_TEMPLATE
                       env var, then 'post_visit_followup').

    Returns:
        Meta API response dict on success.

    Raises:
        requests.HTTPError on API failure.
    """
    name = template_name or os.environ.get(
        "WHATSAPP_POST_VISIT_TEMPLATE", "post_visit_followup"
    )
    url = f"{GRAPH_API}/{_phone_number_id()}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {
            "name": name,
            "language": {"code": "en"},
            "components": [
                {
                    "type": "body",
                    "parameters": [{"type": "text", "text": patient_name}],
                }
            ],
        },
    }
    r = requests.post(url, json=payload, headers=_headers(), timeout=15)
    if not r.ok:
        logger.error("WhatsApp template send failed: %s — %s", r.status_code, r.text)
    r.raise_for_status()
    logger.info("Template '%s' sent to %s", name, to)
    return r.json()


def is_configured() -> bool:
    """Return True if the required env vars are present."""
    return bool(
        os.environ.get("WHATSAPP_PHONE_NUMBER_ID")
        and os.environ.get("WHATSAPP_ACCESS_TOKEN")
    )
