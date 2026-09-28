import json

import requests

OLLAMA_URL = "http://localhost:11434/api/chat"
MODEL = "gpt-oss:20b"

SYSTEM_PROMPT = """You are an email triage assistant for a student. For each email, decide:

1. classification: "actionable" if it has real content from a real person worth acting \
on (assignments, deadlines, meeting/office-hours changes, requests) — "noise" if it's \
an automated receipt, marketing email, newsletter, or notification with nothing to act on. Also mention its category for both
2. topic: a short label for the subject matter (e.g. a course name like "Data Structures"), \
or "general" if it doesn't belong to a specific course/topic.
3. reason: one short sentence explaining the classification.

Respond with ONLY a JSON object with exactly these keys: classification, topic, reason."""


def classify_email(subject, sender, body, max_body_chars=4000):
    user_content = f"From: {sender}\nSubject: {subject}\n\n{body[:max_body_chars]}"

    response = requests.post(
        OLLAMA_URL,
        json={
            "model": MODEL,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            "format": "json",
            "stream": False,
        },
        timeout=120,
    )
    response.raise_for_status()

    content = response.json()["message"]["content"]
    return json.loads(content)
