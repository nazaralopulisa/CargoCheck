"""
comparer.py - compares the SI against the BL for one email.

Input (from the extractor), one dict per email:
    {
      "email_id": "email_009",
      "category": "BL_COMPARISON",
      "si": {7 fields} or None,          # None = SI not found / not readable
      "bl": {7 fields} or None,
      "issues": ["missing_attachment"]   # optional review_reason codes
    }

Output:
    entry  - the dict for submission.json (category, status, review_reason,
             has_defect, defect_fields)
    report - a human-readable result with SI and BL values side by side
"""

from normalizer import NORMALIZERS, normalize_fields, ports_match

FIELDS = list(NORMALIZERS)          # the 7 field names, in a fixed order
REVIEW_REASONS = {"wrong_doc_type", "missing_attachment", "unreadable", "missing_value"}
WEIGHT_TOLERANCE_KG = 0.5           # ignore tiny rounding differences

# If one field is missing but another field clearly mismatches, what do we report?
#   True  -> MISMATCH (we found a real defect, so report it)
#   False -> NEEDS_REVIEW (we can't check everything, so ask a human)
# Try both against the scorer and keep whichever is more accurate.
MISMATCH_BEATS_MISSING = False


# --- Step 1: compare one field ----------------------------------------------

def values_match(field, si_value, bl_value):
    """Compare two already-normalized values."""
    if field == "gross_weight_kg":
        return abs(si_value - bl_value) <= WEIGHT_TOLERANCE_KG
    if field in ("port_of_loading", "port_of_discharge"):
        return ports_match(si_value, bl_value)
    return si_value == bl_value


# --- Step 2: helpers to build the output ------------------------------------

def make_entry(category, status, review_reason=None, defect_fields=None):
    defect_fields = defect_fields or []
    return {
        "category": category,
        "status": status,
        "review_reason": review_reason,
        "has_defect": status == "MISMATCH",
        "defect_fields": defect_fields,
    }


def needs_review(email_id, category, reason, detail):
    entry = make_entry(category, "NEEDS_REVIEW", review_reason=reason)
    report = {"email_id": email_id, "status": "NEEDS_REVIEW",
              "reason": reason, "detail": detail, "fields": []}
    return entry, report


# --- Step 3: compare one whole email ----------------------------------------

def compare_email(extraction):
    email_id = extraction["email_id"]
    category = extraction.get("category", "GENERAL")

    # 3a. Only BL_COMPARISON emails get checked. Everything else is just classified.
    if category != "BL_COMPARISON":
        entry = make_entry(category, "OK")
        report = {"email_id": email_id, "status": "NOT_CHECKED",
                  "detail": f"Classified as {category}; no comparison needed.",
                  "fields": []}
        return entry, report

    # 3b. Problems the extractor already flagged (missing file, unreadable scan...)
    for issue in extraction.get("issues", []):
        if issue in REVIEW_REASONS:
            return needs_review(email_id, category, issue,
                                f"Extractor flagged: {issue}")

    si_raw, bl_raw = extraction.get("si"), extraction.get("bl")
    if si_raw is None or bl_raw is None:
        missing = "SI" if si_raw is None else "BL"
        return needs_review(email_id, category, "missing_attachment",
                            f"No {missing} document found.")

    # 3c. Clean both documents, then go field by field.
    si, bl = normalize_fields(si_raw), normalize_fields(bl_raw)
    mismatched, missing, rows = [], [], []

    for field in FIELDS:
        row = {"field": field, "si": si_raw.get(field), "bl": bl_raw.get(field)}
        if si[field] is None or bl[field] is None:
            row["result"] = "MISSING"
            missing.append(field)
        elif values_match(field, si[field], bl[field]):
            row["result"] = "MATCH"
        else:
            row["result"] = "MISMATCH"
            mismatched.append(field)
        rows.append(row)

    # 3d. Decide the overall status.
    if mismatched and (not missing or MISMATCH_BEATS_MISSING):
        entry = make_entry(category, "MISMATCH", defect_fields=mismatched)
        status, detail = "MISMATCH", f"{len(mismatched)} field(s) differ."
    elif missing:
        entry = make_entry(category, "NEEDS_REVIEW", review_reason="missing_value")
        status, detail = "NEEDS_REVIEW", f"Value not found for: {', '.join(missing)}"
    else:
        entry = make_entry(category, "OK")
        status, detail = "OK", "No mismatch detected."

    if missing and status == "MISMATCH":
        detail += f" Also could not read: {', '.join(missing)}"

    report = {"email_id": email_id, "status": status, "detail": detail, "fields": rows}
    if status == "NEEDS_REVIEW":
        report["reason"] = "missing_value"
    return entry, report


# --- Step 4: print a report a person can read -------------------------------

def format_report(report):
    lines = [f"=== {report['email_id']}: {report['status']} ===", report["detail"]]
    for row in report["fields"]:
        if row["result"] != "MATCH":
            lines.append(f"  {row['field']:<18} SI: {row['si']} / BL: {row['bl']}  [{row['result']}]")
    return "\n".join(lines)


# --- Step 5: self-test -------------------------------------------------------

if __name__ == "__main__":
    base = {
        "shipper": "ASIA PACIFIC PAPERBOARD TRADING PTE LTD",
        "consignee": "PACIFIC OFFICE (M) SDN BHD",
        "notify_party": "PACIFIC OFFICE (M) SDN BHD",
        "port_of_loading": "PORT KLANG (WESTPORT), MALAYSIA (MYPKG)",
        "port_of_discharge": "MERSIN, TURKEY (TRMER)",
        "container_count": "1 x 40'HC",
        "gross_weight_kg": "20,381 KG",
    }

    cases = [
        ("real email_009, all match",
         {"si": base, "bl": dict(base)}, "OK", []),
        ("BL says 4 containers",
         {"si": dict(base, container_count="3 x 40'HC"),
          "bl": dict(base, container_count="4 x 40'HC")}, "MISMATCH", ["container_count"]),
        ("same weight, different units",
         {"si": dict(base, gross_weight_kg="22 MT"),
          "bl": dict(base, gross_weight_kg="22,000 KGS")}, "OK", []),
        ("company name punctuation only",
         {"si": base, "bl": dict(base, consignee="Pacific Office (M) Sdn. Bhd.")}, "OK", []),
        ("weight missing on BL",
         {"si": base, "bl": dict(base, gross_weight_kg=None)}, "NEEDS_REVIEW", []),
        ("BL attachment missing",
         {"si": base, "bl": None}, "NEEDS_REVIEW", []),
        ("extractor flagged unreadable scan",
         {"si": base, "bl": base, "issues": ["unreadable"]}, "NEEDS_REVIEW", []),
    ]

    for name, data, want_status, want_fields in cases:
        extraction = {"email_id": "test", "category": "BL_COMPARISON", **data}
        entry, report = compare_email(extraction)
        ok = entry["status"] == want_status and entry["defect_fields"] == want_fields
        print(f"{'PASS' if ok else 'FAIL'}  {name}: {entry['status']} {entry['defect_fields']}")

    entry, _ = compare_email({"email_id": "x", "category": "SPAM"})
    print(f"{'PASS' if entry['status'] == 'OK' else 'FAIL'}  spam email: not compared")

    print()
    _, report = compare_email({"email_id": "example", "category": "BL_COMPARISON",
                               "si": dict(base, container_count="3 x 40'HC"),
                               "bl": dict(base, container_count="4 x 40'HC")})
    print(format_report(report))
