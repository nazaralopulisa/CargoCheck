import json
import os
import sys
import time
from collections import Counter
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import types

# Find the data folder relative to this file: app/classifier.py -> CargoCheck/data/...
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data" / "sdoc-hackathon-bundle"
OUTPUT_DIR = BASE_DIR / "output"
sys.path.insert(0, str(DATA_DIR))

from loader import Inbox  # type: ignore

load_dotenv()
client = genai.Client()
MODEL = os.getenv("GEMINI_MODEL")

CATEGORIES = ["BL_COMPARISON", "SI_REQUEST", "INVOICE_QUERY", "GENERAL", "SPAM"]

PROMPT = """You are sorting emails in a shipping operations inbox.

Classify the email into exactly one category:

- BL_COMPARISON: the sender wants the team to check, verify, review or confirm
  a draft Bill of Lading (BL) against a Shipping Instruction (SI). This still
  counts even if the attachments are missing or the wording is informal.
- SI_REQUEST: the sender wants a NEW Shipping Instruction prepared, issued or
  submitted. They are not asking to check an existing draft BL.
- INVOICE_QUERY: questions about invoices, charges, payments, freight costs
  or billing.
- GENERAL: operational updates or information with no document check needed,
  e.g. vessel schedules, delays, cut-off reminders, thank-you notes.
- SPAM: unsolicited marketing, phishing, scams, or messages unrelated to this
  team's shipping work, even if they mention shipping words.

Rules:
- Decide from what the sender is actually asking for in the BODY. Subjects can
  be misleading or just reference codes.
- Attachment filenames are a hint, not proof.
- The email may be in any language.

Return JSON in this shape:
{"category": "...", "confidence": "high" | "medium" | "low", "reason": "one short sentence"}

Email:
"""


def classify_email(email):
    """Send one email to Gemini and get back its category, confidence and reason."""
    email_text = (
        f"From: {email.get('from', '')}\n"
        f"Subject: {email.get('subject', '')}\n"
        f"Attachments: {', '.join(email.get('attachments') or []) or 'none'}\n\n"
        f"{email.get('body', '')}"
    )

    response = client.models.generate_content(
        model=MODEL,
        contents=PROMPT + email_text,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0,
        ),
    )
    result = json.loads(response.text)

    # Safety net: if Gemini invents a category, send it for human review
    if result.get("category") not in CATEGORIES:
        result["needs_review"] = True
        result["reason"] = f"Unknown category returned: {result.get('category')}"
    else:
        result["needs_review"] = result.get("confidence") == "low"

    return result


if __name__ == "__main__":
    inbox = Inbox(str(DATA_DIR))
    results = {}

    for email in inbox:
        eid = email["email_id"]
        try:
            results[eid] = classify_email(email)
        except Exception as e:
            results[eid] = {"category": None, "needs_review": True, "reason": f"Error: {e}"}
        print(f"{eid}: {results[eid].get('category')} ({results[eid].get('confidence')})")
        time.sleep(2)  # stay under the free-tier rate limit

    OUTPUT_DIR.mkdir(exist_ok=True)
    with open(OUTPUT_DIR / "classifications.json", "w") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    print("\nCategory counts:", dict(Counter(r.get("category") for r in results.values())))
    flagged = [eid for eid, r in results.items() if r.get("needs_review")]
    print("Needs review:", flagged or "none")