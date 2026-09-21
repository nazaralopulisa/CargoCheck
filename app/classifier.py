import json
import sys
import time
from collections import Counter
from pathlib import Path

from llm import ask_llm_json

# Find the data folder relative to this file: app/classifier.py -> CargoCheck/data/...
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data" / "sdoc-hackathon-bundle"
OUTPUT_DIR = BASE_DIR / "output"
OUTPUT_FILE = OUTPUT_DIR / "classifications.json"
sys.path.insert(0, str(DATA_DIR))

from loader import Inbox  # type: ignore

CATEGORIES = ["BL_COMPARISON", "SI_REQUEST", "INVOICE_QUERY", "GENERAL", "SPAM"]
BATCH_SIZE = 25          # emails per request: fewer requests = faster and cheaper
PAUSE_BETWEEN_CALLS = 2  # seconds, to avoid throttling

PROMPT = """You are sorting emails in a shipping operations inbox.

Classify EACH email below into exactly one category:

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
- Emails may be in any language.
- Classify every email independently. Return one result per email_id.

Return JSON in this shape:
{"results": [
  {"email_id": "...", "category": "...", "confidence": "high" | "medium" | "low",
   "reason": "one short sentence"}
]}

Emails:
"""


def format_email(email):
    return (
        f"=== email_id: {email['email_id']} ===\n"
        f"From: {email.get('from', '')}\n"
        f"Subject: {email.get('subject', '')}\n"
        f"Attachments: {', '.join(email.get('attachments') or []) or 'none'}\n\n"
        f"{email.get('body', '')}\n"
    )


def classify_batch(emails):
    """Classify several emails in ONE request. Returns {email_id: result}."""
    text = "\n".join(format_email(e) for e in emails)
    data = ask_llm_json(PROMPT + text, max_tokens=8000)

    results = {}
    for r in data.get("results", []):
        eid = r.get("email_id")
        if r.get("category") not in CATEGORIES:
            r["needs_review"] = True
            r["reason"] = f"Unknown category returned: {r.get('category')}"
        else:
            r["needs_review"] = r.get("confidence") == "low"
        results[eid] = r
    return results


def load_existing():
    """Load earlier results so a rerun skips emails that already succeeded."""
    if OUTPUT_FILE.exists():
        with open(OUTPUT_FILE) as f:
            return json.load(f)
    return {}


def save(results):
    OUTPUT_DIR.mkdir(exist_ok=True)
    with open(OUTPUT_FILE, "w") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)


if __name__ == "__main__":
    inbox = Inbox(str(DATA_DIR))
    results = load_existing()

    # Only classify emails that don't have a valid result yet
    todo = [e for e in inbox
            if results.get(e["email_id"], {}).get("category") not in CATEGORIES]
    print(f"{len(results)} saved results loaded, {len(todo)} emails left to classify\n")

    for start in range(0, len(todo), BATCH_SIZE):
        batch = todo[start:start + BATCH_SIZE]
        ids = [e["email_id"] for e in batch]

        for attempt in range(4):
            try:
                batch_results = classify_batch(batch)
                results.update(batch_results)
                for eid in ids:
                    if eid in batch_results:
                        r = batch_results[eid]
                        print(f"{eid}: {r.get('category')} ({r.get('confidence')})")
                missing = [i for i in ids if i not in batch_results]
                if missing:
                    print(f"  (not returned, will retry next run: {missing})")
                break
            except Exception as e:
                print(f"Batch {ids[0]}..{ids[-1]} failed (attempt {attempt + 1}): {str(e)[:300]}")
                time.sleep(15 * (attempt + 1))

        save(results)  # save after every batch, so nothing is lost if you stop early
        time.sleep(PAUSE_BETWEEN_CALLS)

    print("\nCategory counts:", dict(Counter(r.get("category") for r in results.values())))
    flagged = [eid for eid, r in results.items() if r.get("needs_review")]
    print("Needs review:", flagged or "none")
    print(f"Saved to {OUTPUT_FILE}")