"""
reviews.py - stores human reviewer decisions and applies them to AI results.

A review never overwrites the AI output. It is saved separately (locally in
output/reviews.json, or in S3 when REVIEWS_BUCKET is set) and layered on top, so you can always see what the AI
said and what the person changed.

    {
      "email_031": {
        "category": "BL_COMPARISON",            # optional: corrected category
        "si": {"gross_weight_kg": "22000"},      # optional: corrected SI values
        "bl": {"consignee": "ABC SDN BHD"},      # optional: corrected BL values
        "decision": "corrected" | "confirmed",
        "note": "Weight was unreadable on the scan, checked the PDF by hand",
        "reviewed_at": "2026-09-21 14:05"
      }
    }
"""

import json
import os
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
REVIEWS_FILE = BASE_DIR / "output" / "reviews.json"

# On AWS, set REVIEWS_BUCKET so reviews survive server restarts. Locally, leave
# it unset and reviews are kept in output/reviews.json as before.
REVIEWS_BUCKET = os.environ.get("REVIEWS_BUCKET")
REVIEWS_KEY = os.environ.get("REVIEWS_KEY", "reviews.json")


def _s3():
    import boto3
    return boto3.client("s3")


def load_reviews():
    if REVIEWS_BUCKET:
        try:
            body = _s3().get_object(Bucket=REVIEWS_BUCKET, Key=REVIEWS_KEY)["Body"].read()
            return json.loads(body)
        except Exception as e:
            if "NoSuchKey" in str(e) or "Not Found" in str(e):
                return {}                      # no reviews saved yet
            raise
    if REVIEWS_FILE.exists():
        return json.loads(REVIEWS_FILE.read_text())
    return {}


def _write(reviews):
    text = json.dumps(reviews, indent=2, ensure_ascii=False)
    if REVIEWS_BUCKET:
        _s3().put_object(Bucket=REVIEWS_BUCKET, Key=REVIEWS_KEY, Body=text.encode(),
                         ContentType="application/json")
    else:
        REVIEWS_FILE.parent.mkdir(exist_ok=True)
        REVIEWS_FILE.write_text(text)


def save_review(email_id, review):
    reviews = load_reviews()
    review = dict(review, reviewed_at=datetime.now().strftime("%Y-%m-%d %H:%M"))
    reviews[email_id] = review
    _write(reviews)
    return review


def delete_review(email_id):
    reviews = load_reviews()
    if reviews.pop(email_id, None) is not None:
        _write(reviews)


def apply_review(extraction, review):
    """Return the AI extraction with the reviewer's corrections layered on top."""
    if not review:
        return extraction
    merged = dict(extraction or {})
    if review.get("category"):
        merged["category"] = review["category"]
    for doc in ("si", "bl"):
        if review.get(doc):
            merged[doc] = {**(merged.get(doc) or {}), **review[doc]}
    # A person has looked at it and supplied the values, so the case is resolved.
    if review.get("decision") == "corrected":
        merged["issues"] = []
    merged["reviewed"] = True
    return merged
