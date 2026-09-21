"""
export.py - turns the dashboard's results into files staff can read and review.

    rows = export_rows(results, labels)      # one plain-language row per email
    export_csv(rows)   -> bytes (opens in Excel / Google Sheets)
    export_json(rows)  -> bytes (for other systems)

Each row answers: what is this email, what did CargoCheck find, how confident is it,
what should a person do, and has a person already verified it?

Confidence is based on HOW the result was reached, not on the model's opinion of
itself (LLMs rate almost everything "high"):
    Verified  a person has reviewed and confirmed or corrected it
    High      all 7 fields read by exact rules from clearly titled documents
    Medium    the AI (LLM) read the documents because the rules couldn't
    Low       a scan read by AI vision, or the system couldn't decide
"""
import csv
import io
import json
from datetime import datetime

from pipeline import scanned_files

FIELDS = ["shipper", "consignee", "notify_party", "port_of_loading",
          "port_of_discharge", "container_count", "gross_weight_kg"]
OPEN = {"NEEDS_REVIEW", "PROCESSING_ERROR", "PENDING"}


def read_by(r):
    if r["data"].get("category") != "BL_COMPARISON" or not r["email"].get("attachments"):
        return ""
    if scanned_files(r["email"]):
        return "AI vision (scanned document)"
    return {"AI": "AI (LLM)", "rules": "Rules (no AI)"}.get(r["data"].get("method"), "")


def confidence(r):
    """(level, reason) for one email."""
    status, data = r["status"], r["data"]
    if status == "PENDING":
        return "", "Not processed yet"
    if r.get("review"):
        return "Verified", "Reviewed by a person"
    if data.get("category") != "BL_COMPARISON":
        level = (data.get("classifier_confidence") or "high").capitalize()
        return level, "Sorting only, no documents to compare"
    if status == "NO_DOCUMENTS":
        return "High", "No documents attached yet, nothing to compare"
    how = read_by(r)
    if how.startswith("AI vision"):
        return "Low", "Scanned document read by AI vision, a person must confirm"
    if status in OPEN:
        return "Low", "The system could not decide on its own"
    if how == "AI (LLM)":
        return "Medium", "Documents read by the AI because the rules could not read them fully"
    return "High", "All 7 fields read by exact rules from clearly titled documents"


def export_rows(results, labels):
    """labels: dict with 'status', 'category', 'field', 'reason', 'action' label maps."""
    rows = []
    for eid, r in sorted(results.items()):
        email, data, entry, report, review = (r["email"], r["data"], r["entry"] or {},
                                              r["report"] or {}, r.get("review") or {})
        fields = {row["field"]: row for row in report.get("fields") or []}
        defects = entry.get("defect_fields") or []
        reason = report.get("reason") or entry.get("review_reason")
        level, why = confidence(r)

        if r["status"] == "MISMATCH":
            action = "Ask for the draft BL to be amended"
        elif r["status"] in ("NEEDS_REVIEW", "PROCESSING_ERROR"):
            action = labels["action"].get(reason, "A person needs to check this")
        elif r["status"] == "PENDING":
            action = "Run the pipeline to process this email"
        else:
            action = "No action needed"

        row = {
            "Email ID": eid,
            "From": email.get("from", ""),
            "Subject": email.get("subject", ""),
            "Email type": labels["category"].get(data.get("category"), ""),
            "Result": labels["status"].get(r["status"], r["status"]),
            "Action needed": "No" if action == "No action needed" else "Yes",
            "What to do": action,
            "Fields to fix": ", ".join(labels["field"][f] for f in defects),
            "Discrepancies": "; ".join(
                f'{labels["field"][f]}: SI "{fields[f]["si"]}" vs BL "{fields[f]["bl"]}"'
                for f in defects if f in fields),
            "Review reason": labels["reason"].get(reason, "") if reason else "",
            "Confidence": level,
            "Confidence reason": why,
            "Documents read by": read_by(r),
            "Why this email type": data.get("classifier_reason") or "",
            "Verified by a person": "Yes" if review else "No",
            "Reviewer": review.get("reviewed_by", ""),
            "Reviewer decision": review.get("decision", ""),
            "Reviewer note": review.get("note", ""),
            "Reviewed at": review.get("reviewed_at", ""),
        }
        for f in FIELDS:
            row[f'{labels["field"][f]} (SI)'] = "" if f not in fields else fields[f]["si"]
            row[f'{labels["field"][f]} (BL)'] = "" if f not in fields else fields[f]["bl"]
            row[f'{labels["field"][f]} result'] = "" if f not in fields else fields[f]["result"]
        rows.append(row)
    return rows


def export_csv(rows):
    buf = io.StringIO()
    if rows:
        writer = csv.DictWriter(buf, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return ("\ufeff" + buf.getvalue()).encode("utf-8")   # BOM so Excel shows symbols correctly


def export_json(rows):
    payload = {"exported_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
               "total_emails": len(rows), "emails": rows}
    return json.dumps(payload, indent=2, ensure_ascii=False, default=str).encode("utf-8")


def filename(kind, scope="all"):
    return f"cargocheck_{scope}_{datetime.now().strftime('%Y%m%d_%H%M')}.{kind}"