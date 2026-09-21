"""
pipeline.py - runs the whole CargoCheck flow, builds submission.json and scores it.

    classifications.json -> extract (BL_COMPARISON only) -> compare -> validate -> submit

Extraction is RULES FIRST, LLM SECOND:
    1. rule_extractor.py reads the SI and BL with plain code (free, instant)
    2. only if the rules couldn't read everything confidently, the LLM is asked
    3. if the LLM is unavailable (quota, account, network), the rules result is
       kept and marked needs_llm, so a later run can finish it

Usage (from the CargoCheck folder):
    python app/pipeline.py                                  # full run, build files only
    python app/pipeline.py --submit --note "what changed"   # full run + score + log
    python app/pipeline.py --rules-only --submit --note ".." # never call the LLM
    python app/pipeline.py --redo-rules --rules-only        # redo rule results after a rules fix
    python app/pipeline.py --skip-extraction --submit       # classification-only baseline
    python app/pipeline.py --only email_009                 # one email, for debugging

Comparison emails with NO attachments are never extracted. Instead:
    - SI details written in the body            -> reclassified as SI_REQUEST
    - body says documents are attached, but none -> NEEDS_REVIEW (missing_attachment)
    - just asking for a draft BL                 -> OK, nothing to compare yet

Extraction results are cached in output/extractions/, so reruns only do emails
not yet extracted, or rule results still waiting for the LLM. Every scored run
is logged to output/score_log.csv with your note.

Settings come from .env (never hardcode localhost):
  DATA_DIR    default: data/sdoc-hackathon-bundle
  SCORER_URL  default: http://localhost:8080
"""
import argparse
import csv
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

import re  # noqa: E402

from comparer import compare_email, format_report  # noqa: E402
from doc_reader import SmartInbox  # noqa: E402
from rule_extractor import extract_email as rule_extract_email, is_confident  # noqa: E402

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = os.environ.get("DATA_DIR", str(BASE_DIR / "data" / "sdoc-hackathon-bundle"))
SCORER_URL = os.environ.get("SCORER_URL", "http://localhost:8080")
OUTPUT_DIR = BASE_DIR / "output"
CLASSIFICATIONS_FILE = OUTPUT_DIR / "classifications.json"
EXTRACTIONS_DIR = OUTPUT_DIR / "extractions"
SUBMISSION_FILE = OUTPUT_DIR / "submission.json"
REPORTS_FILE = OUTPUT_DIR / "reports.json"
SCORE_LOG = OUTPUT_DIR / "score_log.csv"
LAST_SCORE_FILE = OUTPUT_DIR / "last_score.json"

sys.path.insert(0, DATA_DIR)  # so Python can find the organizers' loader.py
from loader import Inbox  # noqa: E402

CATEGORIES = {"BL_COMPARISON", "SI_REQUEST", "INVOICE_QUERY", "GENERAL", "SPAM"}
STATUSES = {"OK", "MISMATCH", "NEEDS_REVIEW"}
REVIEW_REASONS = {"wrong_doc_type", "missing_attachment", "unreadable", "missing_value"}


# --- Step 1: extraction - rules first, LLM second, cached ---------------------

class LLM:
    """Loads the LLM extractor only when it's first needed, and switches it off
    for the rest of the run if the daily quota / account access fails."""
    lock = threading.Lock()
    available = True
    reason = None
    _extract = None

    @classmethod
    def extract(cls, inbox, email):
        with cls.lock:
            if not cls.available:
                raise RuntimeError(cls.reason)
            if cls._extract is None:
                try:
                    from extractor import extract_email
                    cls._extract = extract_email
                except Exception as e:
                    cls.switch_off(f"could not load the LLM extractor: {e}")
                    raise
        return cls._extract(inbox, email)

    @classmethod
    def switch_off(cls, reason):
        with cls.lock:
            if cls.available:
                cls.available, cls.reason = False, reason
                print(f"\n  LLM switched off for this run ({reason[:120]}). "
                      "Continuing with rules only.\n")


def is_permanent_llm_error(message):
    """Errors that won't fix themselves by retrying in a few seconds."""
    m = message.lower()
    return any(s in m for s in ("per day", "perday", "being verified", "accessdenied",
                                "access denied", "not authorized", "no api key",
                                "invalid api key", "credentials"))


def cached_extraction(email_id, rules_only, redo_rules):
    path = EXTRACTIONS_DIR / f"{email_id}.json"
    if not path.exists():
        return None
    cached = json.loads(path.read_text())
    if redo_rules and cached.get("method") != "llm":
        return None                      # rules were improved: redo this one
    if cached.get("needs_llm") and not rules_only and LLM.available:
        return None                      # rules couldn't finish it: try the LLM now
    return cached


def save_extraction(email_id, result):
    EXTRACTIONS_DIR.mkdir(parents=True, exist_ok=True)
    (EXTRACTIONS_DIR / f"{email_id}.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False))


def run_extraction(inbox, email, rules_only, attempts=3):
    """Rules first. LLM only if the rules weren't confident. Never loses the
    rules result: if the LLM fails, the rules result is kept (needs_llm=True)."""
    eid = email["email_id"]
    try:
        result = rule_extract_email(inbox, email)
    except Exception as e:
        return {"email_id": eid, "processing_error": f"rule extractor crashed: {e}"}

    confident = is_confident(result)
    result["needs_llm"] = not confident
    if confident or rules_only or not LLM.available:
        save_extraction(eid, result)
        return result

    error = None
    for attempt in range(attempts):
        try:
            llm_result = LLM.extract(inbox, email)
            llm_result["method"], llm_result["needs_llm"] = "llm", False
            save_extraction(eid, llm_result)
            return llm_result
        except Exception as e:
            error = str(e)
            if is_permanent_llm_error(error) or not LLM.available:
                LLM.switch_off(error)
                break
            time.sleep(5 * (attempt + 1))

    result["llm_error"] = error          # keep the rules result, visibly marked
    save_extraction(eid, result)
    return result


# --- Step 1b: comparison emails that have no attachments ---------------------

BANNER = re.compile(r"^\s*WARNING:.*?originated outside.*?(\n\s*\n|$)", re.I | re.S)
QUOTED_REPLY = re.compile(r"\n\s*(_{5,}|-{5,}\s*Original Message|From:\s.*\n\s*Sent:)", re.I)
CLAIMS_ATTACHMENT = re.compile(r"\b(attach\w*|enclos\w*|please find|pfa|herewith)\b", re.I)
SI_LABELS = [r"\bPOL\b|port of loading", r"\bPOD\b|port of discharge",
             r"\bshipper\b", r"\bconsignee\b", r"notify party"]


def new_message_text(email):
    """The sender's own words: security banner removed, quoted older replies cut off."""
    body = BANNER.sub("", email.get("body", ""), count=1)
    return QUOTED_REPLY.split(body, maxsplit=1)[0]


def has_attachments(email):
    return bool(email.get("attachments"))


def refine_category(email, category):
    """Fix a known pattern the classifier gets wrong: shipping instructions typed
    into the email body (no attachments) are a new SI, not a BL check."""
    if category != "BL_COMPARISON" or has_attachments(email):
        return category, None
    text = new_message_text(email)
    hits = sum(bool(re.search(p, text, re.I)) for p in SI_LABELS)
    if hits >= 3:
        return "SI_REQUEST", (f"Rule: no attachments, and the body contains shipping "
                              f"instruction details ({hits} of 5 SI fields), so this is a new SI.")
    return category, None


def no_attachment_result(email_id, email):
    """Comparison email with nothing attached: escalate only if the sender says
    something was attached; otherwise it's a request for a draft BL."""
    text = new_message_text(email)
    claim = CLAIMS_ATTACHMENT.search(text)
    if claim:
        entry = {"category": "BL_COMPARISON", "status": "NEEDS_REVIEW",
                 "review_reason": "missing_attachment", "has_defect": False, "defect_fields": []}
        report = {"email_id": email_id, "status": "NEEDS_REVIEW", "reason": "missing_attachment",
                  "detail": f'The sender says documents are attached ("{claim.group(0)}") '
                            f"but the email has no attachments.", "fields": []}
    else:
        entry = {"category": "BL_COMPARISON", "status": "OK",
                 "review_reason": None, "has_defect": False, "defect_fields": []}
        report = {"email_id": email_id, "status": "NO_DOCUMENTS",
                  "detail": "Request for a draft BL. No documents attached yet, so there is "
                            "nothing to compare.", "fields": []}
    return entry, report


# --- Step 2: translate extractor output into comparer input -----------------

def issue_codes(extraction):
    """Turn the extractor's sentences into the comparer's review_reason codes.

    Codes are only produced when the SI or BL is actually missing, so an email
    with a valid SI, a valid BL and an extra invoice attached still gets compared.
    """
    if extraction.get("si") and extraction.get("bl"):
        return []

    messages = " | ".join(extraction.get("extraction_issues", [])).lower()
    if "no attachments" in messages:
        return ["missing_attachment"]
    if "could not read" in messages or "empty or unreadable" in messages:
        return ["unreadable"]
    if "was detected as" in messages:
        return ["wrong_doc_type"]
    return ["missing_attachment"]


def to_comparer_input(email_id, category, extraction):
    def fields(doc):
        return doc.get("fields") if doc else None

    return {
        "email_id": email_id,
        "category": category,
        "si": fields(extraction.get("si")),
        "bl": fields(extraction.get("bl")),
        "issues": issue_codes(extraction),
    }


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
        if e["status"] == "NEEDS_REVIEW" and e["review_reason"] not in REVIEW_REASONS:
            problems.append(f"{eid}: NEEDS_REVIEW without a valid review_reason")
    for p in problems[:10]:
        print("  PROBLEM:", p)
    return not problems


# --- Step 4: summary a person can read --------------------------------------

def print_summary(submission, reports, failed, extractions):
    counts = {}
    for e in submission.values():
        key = f"{e['category']} / {e['status']}"
        if e["review_reason"]:
            key += f" ({e['review_reason']})"
        counts[key] = counts.get(key, 0) + 1
    print("\nResult counts:")
    for key, n in sorted(counts.items()):
        print(f"  {key:<48} {n}")


    mismatches = [r for r in reports.values() if r["status"] == "MISMATCH"]
    if mismatches:
        print(f"\nFirst few mismatches (of {len(mismatches)}):")
        for r in mismatches[:3]:
            print(format_report(r))

    if failed:
        print(f"\nProcessing failed for {len(failed)} emails (rerun to retry): {failed}")


# --- Step 5: submit and log the score ---------------------------------------

def submit(submission, note):
    print(f"\nSubmitting to {SCORER_URL} ...")
    result = Inbox(SCORER_URL).submit(submission)
    LAST_SCORE_FILE.write_text(json.dumps(result, indent=2))

    s1 = result.get("stage1", {})
    s3 = result.get("stage3", {})
    e2e = result.get("end_to_end", {})
    rel = result.get("reliability", {})
    final = result.get("final_score")

    if final is None:
        print("Unexpected scoreboard format, saved raw result to", LAST_SCORE_FILE)
        print(json.dumps(result, indent=2)[:2000])
        return

    print(f"  FINAL SCORE          {final:.4f}")
    print(f"  classification F1    {s1.get('macro_f1', 0):.3f}")
    print(f"  defect F1            {s3.get('defect_f1', 0):.3f}")
    print(f"  end-to-end           {e2e.get('success')}/{e2e.get('total')}")
    print(f"  escalation recall    {rel.get('escalation_recall', 0):.3f}")

    # Keep a history of every run: evidence of how the system improved.
    new_file = not SCORE_LOG.exists()
    with SCORE_LOG.open("a", newline="") as f:
        writer = csv.writer(f)
        if new_file:
            writer.writerow(["time", "final", "class_f1", "defect_f1",
                             "e2e", "escalation_recall", "note"])
        writer.writerow([datetime.now().strftime("%Y-%m-%d %H:%M"),
                         f"{final:.4f}", f"{s1.get('macro_f1', 0):.3f}",
                         f"{s3.get('defect_f1', 0):.3f}",
                         f"{e2e.get('success')}/{e2e.get('total')}",
                         f"{rel.get('escalation_recall', 0):.3f}", note])
    print(f"  (logged to {SCORE_LOG})")


# --- Step 6: run everything --------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-extraction", action="store_true",
                        help="classification-only baseline: every email is marked OK")
    parser.add_argument("--rules-only", action="store_true",
                        help="never call the LLM for extraction (free, no quota)")
    parser.add_argument("--redo-rules", action="store_true",
                        help="re-run the rule extractor on emails it already did")
    parser.add_argument("--submit", action="store_true", help="send to the scorer")
    parser.add_argument("--note", default="", help="what changed in this run")
    parser.add_argument("--only", help="process a single email_id")
    parser.add_argument("--workers", type=int, default=4,
                        help="how many emails to extract at the same time")
    args = parser.parse_args()

    classifications = json.loads(CLASSIFICATIONS_FILE.read_text())

    inbox = SmartInbox(Inbox(DATA_DIR), DATA_DIR)  # reads PDF, Word, Excel and scans too
    emails = {e["email_id"]: e for e in inbox}
    if args.only:
        emails = {args.only: emails[args.only]}

    missing_class = [eid for eid in emails if eid not in classifications]
    if missing_class:
        print(f"WARNING: {len(missing_class)} emails have no classification, "
              f"treated as GENERAL: {missing_class[:10]}")

    categories, rule_notes = {}, {}
    for eid, email in emails.items():
        original = classifications.get(eid, {}).get("category") or "GENERAL"
        categories[eid], note = refine_category(email, original)
        if note:
            rule_notes[eid] = note
    if rule_notes:
        print(f"Reclassified by rule: {len(rule_notes)} emails -> {sorted(rule_notes)}")

    def category_of(eid):
        return categories[eid]

    # Extract BL_COMPARISON emails that are not cached yet
    extractions = {}
    to_compare = [eid for eid in emails
                  if category_of(eid) == "BL_COMPARISON" and has_attachments(emails[eid])]

    if not args.skip_extraction:
        todo = []
        for eid in to_compare:
            cached = cached_extraction(eid, args.rules_only, args.redo_rules)
            if cached:
                extractions[eid] = cached
            else:
                todo.append(eid)

        mode = "rules only" if args.rules_only else "rules first, LLM when needed"
        print(f"{len(to_compare)} comparison emails: {len(extractions)} cached, "
              f"{len(todo)} to extract ({mode})\n")

        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(run_extraction, inbox, emails[eid], args.rules_only): eid
                       for eid in todo}
            for i, future in enumerate(as_completed(futures), 1):
                eid = futures[future]
                x = extractions[eid] = future.result()
                if "processing_error" in x:
                    flag = "FAILED"
                elif x.get("method") == "llm":
                    flag = "llm"
                else:
                    flag = "rules, waiting for LLM" if x.get("needs_llm") else "rules"
                print(f"[{i}/{len(todo)}] {eid}: {flag}")

    # Compare and build the submission
    submission, reports, failed = {}, {}, []
    for eid in sorted(emails):
        category = category_of(eid)

        if category == "BL_COMPARISON" and not has_attachments(emails[eid]):
            entry, report = no_attachment_result(eid, emails[eid])
        elif category == "BL_COMPARISON" and args.skip_extraction:
            entry, report = compare_email({"email_id": eid, "category": "GENERAL"})
            entry["category"] = "BL_COMPARISON"
        elif category == "BL_COMPARISON" and "processing_error" in extractions.get(eid, {}):
            failed.append(eid)
            entry = {"category": category, "status": "NEEDS_REVIEW",
                     "review_reason": "unreadable", "has_defect": False, "defect_fields": []}
            report = {"email_id": eid, "status": "PROCESSING_ERROR",
                      "detail": extractions[eid]["processing_error"], "fields": []}
        elif category == "BL_COMPARISON":
            entry, report = compare_email(to_comparer_input(eid, category, extractions[eid]))
        else:
            entry, report = compare_email({"email_id": eid, "category": category})

        report["classification_reason"] = (rule_notes.get(eid)
                                           or classifications.get(eid, {}).get("reason"))
        submission[eid] = entry
        reports[eid] = report

    if not validate(submission):
        sys.exit("Fix the problems above before submitting.")

    OUTPUT_DIR.mkdir(exist_ok=True)
    SUBMISSION_FILE.write_text(json.dumps(submission, indent=2))
    REPORTS_FILE.write_text(json.dumps(reports, indent=2, ensure_ascii=False))
    print(f"\nWrote {SUBMISSION_FILE} and {REPORTS_FILE}")
    print_summary(submission, reports, failed, extractions)

    if args.only:
        print("\n" + format_report(reports[args.only]))

    if args.submit:
        if args.only:
            print("\nNot submitting: --only builds a partial submission.")
        else:
            submit(submission, args.note)


if __name__ == "__main__":
    main()