"""
pipeline.py - runs every email through classify -> extract -> adapt,
and saves extractions.json for make_submission.py.

    python app/pipeline.py               # process everything not done yet
    python app/pipeline.py --limit 20    # only the first 20 emails (testing)
    python app/pipeline.py --only email_009  # just one email

Nothing is paid for twice:
  - classifications are shared with app/classifier.py (output/classifications.json),
    so emails your partner already classified are reused, not re-sent to Gemini
  - raw extractor results are saved in output/extraction_cache.json
Progress is saved after every batch/email. Failed ones are retried on the next run.
"""

import argparse
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent   # app/pipeline.py -> CargoCheck/
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR / "data" / "sdoc-hackathon-bundle"))
OUTPUT_DIR = BASE_DIR / "output"
EXTRACTION_CACHE = OUTPUT_DIR / "extraction_cache.json"
EXTRACTIONS_FILE = BASE_DIR / "extractions.json"
PAUSE_SECONDS = float(os.environ.get("PAUSE_SECONDS", "6"))

sys.path.insert(0, str(DATA_DIR))
from loader import Inbox                                        # noqa: E402
from classifier import (classify_batch, CATEGORIES, BATCH_SIZE,  # noqa: E402
                            OUTPUT_FILE as CLASSIFICATIONS_FILE)
from extractor import extract_email                         # noqa: E402


class DailyQuotaReached(Exception):
    pass


# --- Step 1: retry busy/rate-limited calls automatically ---------------------

def with_retries(fn, *args, attempts=4):
    """Retry temporary errors (503 busy, 429 per-minute limit) with growing waits.
    Stop immediately if the DAILY quota is used up."""
    for attempt in range(1, attempts + 1):
        try:
            return fn(*args)
        except Exception as e:
            message = str(e)
            if "per day" in message.lower() or "perday" in message.lower():
                raise DailyQuotaReached(message)
            if attempt == attempts:
                raise
            wait = 20 * attempt
            print(f"    attempt {attempt} failed ({message[:80]}...) - retrying in {wait}s")
            time.sleep(wait)


# --- Step 2: saving and loading ---------------------------------------------

def load_json(path):
    return json.loads(path.read_text()) if path.exists() else {}


def save_json(path, data):
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))


# --- Step 3: classification (batches, shared file with classifier.py) --------

def classify_missing(emails, classifications):
    todo = [e for e in emails
            if classifications.get(e["email_id"], {}).get("category") not in CATEGORIES]
    print(f"\nCLASSIFY: {len(emails) - len(todo)} already done, {len(todo)} to do")
    for start in range(0, len(todo), BATCH_SIZE):
        batch = todo[start:start + BATCH_SIZE]
        ids = [e["email_id"] for e in batch]
        try:
            results = with_retries(classify_batch, batch)
            classifications.update(results)
            save_json(CLASSIFICATIONS_FILE, classifications)
            done = [i for i in ids if i in results]
            print(f"  {ids[0]}..{ids[-1]}: {len(done)}/{len(ids)} classified")
        except DailyQuotaReached:
            raise
        except Exception as e:
            print(f"  {ids[0]}..{ids[-1]}: FAILED - {str(e)[:120]} (will retry next run)")
        time.sleep(PAUSE_SECONDS)


# --- Step 4: extraction (only BL_COMPARISON emails) -------------------------

def extract_missing(inbox, emails, classifications, cache):
    todo = [e for e in emails
            if classifications.get(e["email_id"], {}).get("category") == "BL_COMPARISON"
            and e["email_id"] not in cache]
    print(f"\nEXTRACT: {len(todo)} BL_COMPARISON emails to extract")
    for i, email in enumerate(todo, 1):
        eid = email["email_id"]
        try:
            cache[eid] = with_retries(extract_email, inbox, email)
            save_json(EXTRACTION_CACHE, cache)
            issues = cache[eid].get("extraction_issues") or []
            print(f"  [{i}/{len(todo)}] {eid}: done" + (f"  ({issues[0]})" if issues else ""))
        except DailyQuotaReached:
            raise
        except Exception as e:
            print(f"  [{i}/{len(todo)}] {eid}: FAILED - {str(e)[:120]} (will retry next run)")
        time.sleep(PAUSE_SECONDS)


# --- Step 5: the adapter (partner's format -> comparer's format) -------------

def issue_codes(email, extraction):
    if not email.get("attachments"):
        return ["missing_attachment"]
    if extraction.get("si") is not None and extraction.get("bl") is not None:
        return []
    messages = " ".join(extraction.get("extraction_issues", [])).lower()
    if extraction.get("other_documents"):
        return ["wrong_doc_type"]
    if "could not read" in messages or "empty or unreadable" in messages:
        return ["unreadable"]
    return ["missing_attachment"]


def fields_of(doc):
    return None if doc is None else (doc.get("fields") or {})


def adapt(email, classification, extraction):
    entry = {
        "category": classification["category"],
        "classifier_confidence": classification.get("confidence"),
        "classifier_reason": classification.get("reason"),
        "classifier_needs_review": classification.get("needs_review", False),
    }
    if classification["category"] == "BL_COMPARISON":
        if extraction is None and not email.get("attachments"):
            extraction = {"si": None, "bl": None, "extraction_issues": ["No attachments"]}
        if extraction is not None:
            entry["si"] = fields_of(extraction.get("si"))
            entry["bl"] = fields_of(extraction.get("bl"))
            entry["issues"] = issue_codes(email, extraction)
            entry["issue_notes"] = extraction.get("extraction_issues", [])
    return entry


def build_extractions(emails, classifications, cache):
    out, not_ready = {}, []
    for email in emails:
        eid = email["email_id"]
        c = classifications.get(eid, {})
        if c.get("category") not in CATEGORIES:
            not_ready.append(eid)
            continue
        entry = adapt(email, c, cache.get(eid))
        if c["category"] == "BL_COMPARISON" and "si" not in entry:
            not_ready.append(eid)          # classified but not extracted yet
            continue
        out[eid] = entry
    return out, not_ready


# --- Step 6: run everything -------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, help="only the first N emails")
    parser.add_argument("--only", help="a single email_id")
    args = parser.parse_args()

    inbox = Inbox(str(DATA_DIR))
    emails = list(inbox)
    if args.only:
        emails = [e for e in emails if e["email_id"] == args.only]
    if args.limit:
        emails = emails[:args.limit]

    classifications = load_json(CLASSIFICATIONS_FILE)
    cache = load_json(EXTRACTION_CACHE)

    try:
        classify_missing(emails, classifications)
        extract_missing(inbox, emails, classifications, cache)
    except DailyQuotaReached:
        print("\nDAILY QUOTA USED UP. Everything so far is saved - rerun after it resets.")
    except KeyboardInterrupt:
        print("\nStopped. Everything so far is saved - rerun to continue.")

    # Always write whatever is finished, even after a stop.
    all_emails = list(inbox)
    extractions, not_ready = build_extractions(all_emails, classifications, cache)
    save_json(EXTRACTIONS_FILE, extractions)

    print("\nSUMMARY")
    print(f"  ready for comparison: {len(extractions)} / {len(all_emails)} emails")
    print(f"  not finished yet:     {len(not_ready)} (placeholder used until done)")
    print("  categories:", dict(Counter(e['category'] for e in extractions.values())))
    review = [eid for eid, e in extractions.items() if e.get("issues")]
    print(f"  documents needing review: {len(review)}")
    print(f"\nSaved {EXTRACTIONS_FILE.name}. Next: python app/make_submission.py --submit")


if __name__ == "__main__":
    main()
