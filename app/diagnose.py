"""
diagnose.py - explains why comparison emails were sent to NEEDS_REVIEW.

For every email flagged missing_attachment or wrong_doc_type in output/submission.json,
it writes to output/diagnosis.txt:
  - the attachments listed in the email JSON
  - any files on disk whose name starts with the email_id (in case they aren't listed)
  - other emails that share a reference number (OC / BL / booking) and DO have attachments
  - the first lines of each attachment, and whether it could be read as text
  - what the extractor recorded (issues, detected document types)

Usage (from the CargoCheck folder):
    python app/diagnose.py
Then open output/diagnosis.txt, or share it.
"""
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("DATA_DIR", str(BASE_DIR / "data" / "sdoc-hackathon-bundle")))
OUTPUT_DIR = BASE_DIR / "output"
sys.path.insert(0, str(DATA_DIR))
from loader import Inbox  # noqa: E402

# Reference numbers seen in subjects, e.g. 5AAT-03056, SIN525534192, MEDUUD104332
REF_PATTERN = re.compile(r"\b(\d[A-Z]{3}-\d{5}|[A-Z]{3,6}\d{6,})\b")


def refs(email):
    text = f"{email.get('subject', '')} {email.get('body', '')}".upper()
    return set(REF_PATTERN.findall(text))


def first_lines(inbox, path, n=6):
    try:
        text = inbox.read_text(path)
    except Exception as e:
        return f"      COULD NOT READ AS TEXT: {str(e)[:150]}"
    if not text or not text.strip():
        return "      (empty text)"
    lines = [l for l in text.splitlines() if l.strip()][:n]
    return "\n".join(f"      | {l[:110]}" for l in lines)


def main():
    submission = json.loads((OUTPUT_DIR / "submission.json").read_text())
    inbox = Inbox(str(DATA_DIR))
    emails = {e["email_id"]: e for e in inbox}

    attach_dir = DATA_DIR / "attachments"
    all_files = sorted(p.name for p in attach_dir.iterdir()) if attach_dir.exists() else []
    ext_counts = Counter(Path(f).suffix.lower() or "(none)" for f in all_files)

    ref_index = {}
    for eid, e in emails.items():
        if e.get("attachments"):
            for r in refs(e):
                ref_index.setdefault(r, []).append(eid)

    flagged = {eid: v["review_reason"] for eid, v in submission.items()
               if v["review_reason"] in ("missing_attachment", "wrong_doc_type")}

    out = []
    out.append(f"Attachment files on disk: {len(all_files)}  by type: {dict(ext_counts)}")
    out.append(f"Flagged emails: {len(flagged)}  {dict(Counter(flagged.values()))}\n")

    summary = Counter()
    for eid, reason in sorted(flagged.items()):
        e = emails[eid]
        listed = e.get("attachments") or []
        on_disk = [f for f in all_files if f.startswith(eid + "_") or f.startswith(eid + ".")]
        unlisted = [f for f in on_disk if not any(l.endswith(f) for l in listed)]
        related = sorted({o for r in refs(e) for o in ref_index.get(r, []) if o != eid})

        if not listed and unlisted:
            summary["no attachments listed, but files exist on disk"] += 1
        elif not listed and related:
            summary["no attachments, but a related email has them"] += 1
        elif not listed:
            summary["no attachments anywhere"] += 1
        else:
            exts = sorted({Path(l).suffix.lower() for l in listed})
            summary[f"has attachments {exts}"] += 1

        out.append("=" * 90)
        out.append(f"{eid}  [{reason}]  subject: {e.get('subject', '')[:80]}")
        out.append(f"  listed attachments : {listed or 'none'}")
        if unlisted:
            out.append(f"  files on disk NOT listed: {unlisted}")
        if related:
            out.append(f"  related emails (shared refs {sorted(refs(e))}): {related[:5]}")

        cache = OUTPUT_DIR / "extractions" / f"{eid}.json"
        if cache.exists():
            x = json.loads(cache.read_text())
            out.append(f"  extractor method   : {x.get('method', 'rules')}"
                       f"   needs_llm: {x.get('needs_llm')}   llm_error: {str(x.get('llm_error'))[:80]}")
            out.append(f"  extractor issues   : {x.get('extraction_issues')}")
            docs = [x.get("si"), x.get("bl")] + (x.get("other_documents") or [])
            types = [(d.get("source_file"), d.get("doc_type_detected")) for d in docs if d]
            out.append(f"  detected doc types : {types}")

        for path in listed + [f"attachments/{f}" for f in unlisted]:
            out.append(f"    {path}")
            out.append(first_lines(inbox, path))

    out.insert(2, "SUMMARY:\n" + "\n".join(f"  {n:>4}  {k}" for k, n in summary.most_common()) + "\n")
    (OUTPUT_DIR / "diagnosis.txt").write_text("\n".join(out), encoding="utf-8")
    print("\n".join(out[:3]))
    print(f"\nFull details written to {OUTPUT_DIR / 'diagnosis.txt'}")


if __name__ == "__main__":
    main()