"""
make_submission.py - runs every email through the comparer, writes
submission.json, and (optionally) sends it to the scoring server.

    python app/make_submission.py                    # build submission.json only
    python app/make_submission.py --submit           # build + score
    python app/make_submission.py --submit --note "tried X"

Where the data comes from:
  - extractions.json : your partner's extractor output, keyed by email_id
                       (same shape as the docstring at the top of comparer.py).
  - any email NOT in extractions.json gets a simple placeholder guess, so you
    can test the whole scoring loop before the extractor is ready.

Settings come from environment variables (never hardcode localhost):
  DATA_DIR    default: data/sdoc-hackathon-bundle
  SCORER_URL  default: http://localhost:8080
"""

import argparse
import csv
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from comparer import compare_email, format_report

BASE_DIR = Path(__file__).resolve().parent.parent   # app/make_submission.py -> CargoCheck/
DATA_DIR = os.environ.get("DATA_DIR", str(BASE_DIR / "data" / "sdoc-hackathon-bundle"))
SCORER_URL = os.environ.get("SCORER_URL", "http://localhost:8080")
EXTRACTIONS_FILE = BASE_DIR / "extractions.json"
SUBMISSION_FILE = BASE_DIR / "submission.json"
REPORTS_FILE = BASE_DIR / "reports.json"
SCORE_LOG = BASE_DIR / "score_log.csv"

sys.path.insert(0, DATA_DIR)     # so Python can find the organizers' loader.py
from loader import Inbox         # noqa: E402

CATEGORIES = {"BL_COMPARISON", "SI_REQUEST", "INVOICE_QUERY", "GENERAL", "SPAM"}
STATUSES = {"OK", "MISMATCH", "NEEDS_REVIEW"}


# --- Step 1: placeholder for emails the extractor hasn't handled yet ---------

def placeholder_entry(email):
    """Rough guess: an email with both an SI and a BL attached is probably a
    comparison request. Replaced automatically once extractions.json has it."""
    names = " ".join(email.get("attachments", [])).upper()
    category = "BL_COMPARISON" if ("_SI" in names and "_BL" in names) else "GENERAL"
    entry = {"category": category, "status": "OK", "review_reason": None,
             "has_defect": False, "defect_fields": []}
    report = {"email_id": email["email_id"], "status": "PLACEHOLDER",
              "detail": "Not extracted yet; placeholder guess.", "fields": []}
    return entry, report


# --- Step 2: build the submission for every email ---------------------------

def build_submission():
    extractions = {}
    if EXTRACTIONS_FILE.exists():
        extractions = json.loads(EXTRACTIONS_FILE.read_text())
    print(f"Loaded {len(extractions)} extraction results from {EXTRACTIONS_FILE}")

    submission, reports = {}, {}
    used_placeholder = 0
    for email in Inbox(DATA_DIR):
        eid = email["email_id"]
        if eid in extractions:
            extraction = dict(extractions[eid], email_id=eid)
            entry, report = compare_email(extraction)
        else:
            entry, report = placeholder_entry(email)
            used_placeholder += 1
        submission[eid] = entry
        reports[eid] = report

    print(f"Processed {len(submission)} emails ({used_placeholder} used the placeholder)")
    return submission, reports


# --- Step 3: check the format before sending --------------------------------

def validate(submission):
    problems = []
    for eid, e in submission.items():
        if e["category"] not in CATEGORIES:
            problems.append(f"{eid}: bad category {e['category']}")
        if e["status"] not in STATUSES:
            problems.append(f"{eid}: bad status {e['status']}")
        if e["has_defect"] != (e["status"] == "MISMATCH"):
            problems.append(f"{eid}: has_defect doesn't match status")
    for p in problems[:10]:
        print("  PROBLEM:", p)
    return not problems


# --- Step 4: summary a person can read --------------------------------------

def print_summary(submission, reports):
    counts = {}
    for e in submission.values():
        key = f"{e['category']} / {e['status']}"
        counts[key] = counts.get(key, 0) + 1
    print("\nResult counts:")
    for key, n in sorted(counts.items()):
        print(f"  {key:<32} {n}")

    mismatches = [r for r in reports.values() if r["status"] == "MISMATCH"]
    if mismatches:
        print(f"\nFirst few mismatches (of {len(mismatches)}):")
        for r in mismatches[:3]:
            print(format_report(r))


# --- Step 5: submit and log the score ---------------------------------------

def submit(submission, note):
    print(f"\nSubmitting to {SCORER_URL} ...")
    result = Inbox(SCORER_URL).submit(submission)
    s1, s3, e2e = result["stage1"], result["stage3"], result["end_to_end"]
    rel = result["reliability"]
    print(f"  FINAL SCORE          {result['final_score']:.4f}")
    print(f"  classification F1    {s1['macro_f1']:.3f}")
    print(f"  defect F1            {s3['defect_f1']:.3f}")
    print(f"  end-to-end           {e2e['success']}/{e2e['total']}")
    print(f"  escalation recall    {rel['escalation_recall']:.3f}")

    # Keep a history of every run: evidence for the Validation criterion.
    new_file = not SCORE_LOG.exists()
    with SCORE_LOG.open("a", newline="") as f:
        writer = csv.writer(f)
        if new_file:
            writer.writerow(["time", "final", "class_f1", "defect_f1",
                             "e2e", "escalation_recall", "note"])
        writer.writerow([datetime.now().strftime("%Y-%m-%d %H:%M"),
                         f"{result['final_score']:.4f}", f"{s1['macro_f1']:.3f}",
                         f"{s3['defect_f1']:.3f}", f"{e2e['success']}/{e2e['total']}",
                         f"{rel['escalation_recall']:.3f}", note])
    print(f"  (logged to {SCORE_LOG})")
    (BASE_DIR / "last_score.json").write_text(json.dumps(result, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--submit", action="store_true", help="send to the scorer")
    parser.add_argument("--note", default="", help="what changed in this run")
    args = parser.parse_args()

    submission, reports = build_submission()
    if not validate(submission):
        sys.exit("Fix the problems above before submitting.")

    SUBMISSION_FILE.write_text(json.dumps(submission, indent=2))
    REPORTS_FILE.write_text(json.dumps(reports, indent=2))
    print(f"Wrote {SUBMISSION_FILE} and {REPORTS_FILE}")
    print_summary(submission, reports)

    if args.submit:
        submit(submission, args.note)
