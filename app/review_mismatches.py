"""
review_mismatches.py - puts the RAW document lines next to the EXTRACTED values
for every MISMATCH email, so you can spot values the extractors misread the same
way on both sides (which the reports alone can't show).

It pre-flags risky patterns:
  - container lines with several groups ("2 x 20'GP + 1 x 40'HC")
  - more than one weight line in a document
  - a party name whose next line might continue the name
Flagged emails are listed first.

Usage (from the CargoCheck folder):
    python app/review_mismatches.py
Then open output/mismatch_review.txt
"""
import json
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("DATA_DIR", str(BASE_DIR / "data" / "sdoc-hackathon-bundle")))
OUTPUT_DIR = BASE_DIR / "output"
sys.path.insert(0, str(DATA_DIR))
from loader import Inbox  # noqa: E402
from doc_reader import SmartInbox  # noqa: E402

FIELD_WORDS = re.compile(r"shipper|exporter|consignee|notify|port|\bpol\b|\bpod\b|load|discharge|"
                         r"container|cntr|gross|weight|\bwt\b|\bkgs?\b", re.I)
WEIGHT_LINE = re.compile(r"gross|weight|\bwt\b", re.I)
MULTI_CONTAINER = re.compile(r"\d+\s*[xX×]\s*\d+.*(\+|&|,|\band\b).*\d+\s*[xX×]", re.I)
PARTY_LABEL = re.compile(r"shipper|consignee|notify", re.I)


def relevant_lines(text):
    """Lines that mention one of the 7 fields, plus the line after each party label
    (where a name may continue)."""
    lines = text.splitlines()
    keep = []
    for i, line in enumerate(lines):
        if FIELD_WORDS.search(line):
            keep.append(line.rstrip())
            if PARTY_LABEL.search(line) and i + 1 < len(lines) and lines[i + 1][:1].isspace():
                keep.append("      ↳ " + lines[i + 1].strip())
    return keep


def flags_for(text):
    flags = []
    if MULTI_CONTAINER.search(text):
        flags.append("several container groups")
    weights = [l for l in text.splitlines() if WEIGHT_LINE.search(l) and re.search(r"\d", l)]
    if len(weights) > 1:
        flags.append(f"{len(weights)} weight lines")
    return flags


def main():
    reports = json.loads((OUTPUT_DIR / "reports.json").read_text())
    inbox = SmartInbox(Inbox(str(DATA_DIR)), DATA_DIR)
    emails = {e["email_id"]: e for e in inbox}

    blocks = []
    for eid, report in sorted(reports.items()):
        if report["status"] != "MISMATCH":
            continue
        out, email_flags = [], []
        out.append(f"{eid}   reported: " + ", ".join(
            f"{r['field']} (SI: {r['si']} / BL: {r['bl']})" for r in report["fields"]
            if r["result"] != "MATCH"))
        out.append("  extracted, all 7 fields:")
        for r in report["fields"]:
            out.append(f"    {r['field']:<18} SI: {str(r['si'])[:55]:<55} BL: {str(r['bl'])[:55]}  [{r['result']}]")

        for path in emails[eid].get("attachments") or []:
            try:
                text = inbox.read_text(path)
            except Exception as e:
                out.append(f"  {path}: could not read ({e})")
                continue
            f = flags_for(text)
            email_flags += [f"{Path(path).name}: {x}" for x in f]
            out.append(f"  --- raw lines from {path}" + (f"   ⚠ {', '.join(f)}" if f else ""))
            out += [f"      {l[:130]}" for l in relevant_lines(text)]
        blocks.append((bool(email_flags), eid, email_flags, out))

    blocks.sort(key=lambda b: (not b[0], b[1]))          # flagged emails first
    flagged = [b for b in blocks if b[0]]
    lines = [f"{len(blocks)} mismatch emails, {len(flagged)} with risky patterns (listed first):"]
    lines += [f"  {eid}: {'; '.join(f)}" for _, eid, f, _ in flagged]
    for _, _, _, out in blocks:
        lines.append("\n" + "=" * 100)
        lines += out
    (OUTPUT_DIR / "mismatch_review.txt").write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines[:len(flagged) + 1]))
    print(f"\nFull side-by-side written to {OUTPUT_DIR / 'mismatch_review.txt'}")


if __name__ == "__main__":
    main()