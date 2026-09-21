import json
import sys
from pathlib import Path

from llm import ask_llm_json

# Find the data folder relative to this file: app/extractor.py -> CargoCheck/data/...
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data" / "sdoc-hackathon-bundle"
sys.path.insert(0, str(DATA_DIR))

from loader import Inbox  # type: ignore

PROMPT = """You are extracting data from a shipping document.

First, identify the document type: "SI" (Shipping Instruction), "BL" (Bill of Lading),
or "OTHER" (anything else, e.g. an invoice).

Then extract these 7 fields, matching by meaning, not by exact label
(e.g. "POD", "Discharge Port" and "Port of Discharge" are the same field):

- shipper: company name only, no address
- consignee: company name only, no address
- notify_party: company name only, no address
- port_of_loading: as written, including any code in brackets
- port_of_discharge: as written, including any code in brackets
- container_count: total number of containers as an integer
  ("1 x 40'HC" = 1, "2 x 20'GP + 1 x 40'HC" = 3)
- gross_weight_kg: number in kilograms as an integer, no commas or units

Rules:
- If a field is not present, return null. Never guess.
- Copy text values exactly as written. Do not correct spelling or reformat.
- Return JSON in this shape:
{"doc_type_detected": "...", "fields": {...}}

Document:
"""


def extract_document(text):
    """Send one document's text to the LLM and get the 7 fields back as a dict."""
    return ask_llm_json(PROMPT + text)


def extract_email(inbox, email):
    """Extract every attachment of one email and sort them into SI and BL."""
    result = {
        "email_id": email["email_id"],
        "si": None,
        "bl": None,
        "other_documents": [],
        "extraction_issues": [],
    }

    attachments = email.get("attachments") or []
    if not attachments:
        result["extraction_issues"].append("No attachments on this email")
        return result

    for path in attachments:
        try:
            text = inbox.read_text(path)
        except Exception as e:
            result["extraction_issues"].append(f"Could not read {path}: {e}")
            continue

        if not text or not text.strip():
            result["extraction_issues"].append(f"{path} is empty or unreadable")
            continue

        doc = extract_document(text)
        doc["source_file"] = path
        doc_type = doc.get("doc_type_detected")

        # Trust what the document actually is, not what the filename says
        if doc_type == "SI" and result["si"] is None:
            result["si"] = doc
        elif doc_type == "BL" and result["bl"] is None:
            result["bl"] = doc
        else:
            result["other_documents"].append(doc)
            result["extraction_issues"].append(
                f"{path} was detected as {doc_type}, not a usable SI/BL"
            )

    if result["si"] is None:
        result["extraction_issues"].append("No SI found")
    if result["bl"] is None:
        result["extraction_issues"].append("No BL found")

    return result


if __name__ == "__main__":
    # Quick test: run on one email. Change the id to try others.
    inbox = Inbox(str(DATA_DIR))
    email = next(e for e in inbox if e["email_id"] == "email_001")
    print(json.dumps(extract_email(inbox, email), indent=2))