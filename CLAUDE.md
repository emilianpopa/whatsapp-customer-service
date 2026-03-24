# WhatsApp Customer Service Bot

## Purpose
AI-powered WhatsApp customer service for ExpandHealth Cape Town clinic.
Scans incoming WhatsApp messages, generates AI responses using a clinic knowledge base,
and presents them in a receptionist dashboard for review/auto-send.

## Architecture
- `scanner.py` — Playwright-based WhatsApp Web message scanner (adapted from whatsapp-analyzer)
- `responder.py` — Claude AI response generator with knowledge base
- `web/app.py` — Flask dashboard for receptionist message queue
- `knowledge_base/` — Clinic information (pricing, hours, services, FAQs)

## Stack
- Python 3.11+, Flask, Anthropic SDK, Playwright
- WhatsApp Web scraping (no API — Playwright session)
- Claude Sonnet for response generation
- SQLite for message queue

## Deployment
- Target: Railway (auto-deploy from GitHub main)
- WhatsApp session stored on persistent volume
- Computer must be open for Playwright session

## Related Projects
- whatsapp-analyzer — Source of WhatsApp scraping patterns
- dawa-ai — Source of AI chatbot / knowledge base patterns
