"""
uploads.py - emails uploaded through the dashboard, kept as part of the inbox.

Stored separately from the organizers' dataset, so the dataset is never changed and
uploads never affect submission.json or the score:

    output/uploads/<email_id>/
        email.json            the email record (attachments point to the files below)
        classification.json   what the classifier (or a person) decided
        extraction.json       what was read from the documents
        files/                the uploaded attachments
"""

import json
import shutil
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
UPLOADS_DIR = BASE_DIR / "output" / "uploads"


def new_upload_id(original_id, taken):
    """uploaded_email_777, or uploaded_email_777_2 if that's taken. Never clashes with
    a real inbox email, even if the same email is uploaded twice."""
    base = f"uploaded_{original_id or 'email'}"
    eid, n = base, 2
    while eid in taken or (UPLOADS_DIR / eid).exists():
        eid, n = f"{base}_{n}", n + 1
    return eid


def save_upload(eid, email, files, classification, extraction):
    """Save (or overwrite) one uploaded email and its attachments."""
    folder = UPLOADS_DIR / eid
    if folder.exists():
        shutil.rmtree(folder)
    (folder / "files").mkdir(parents=True)
    attachments = []
    for path, data in files.items():
        name = Path(path).name
        (folder / "files" / name).write_bytes(data)
        attachments.append(f"uploads/{eid}/{name}")
    record = dict(email, email_id=eid, attachments=attachments,
                  original_email_id=email.get("email_id"),
                  uploaded_at=datetime.now().strftime("%Y-%m-%d %H:%M"))
    (folder / "email.json").write_text(json.dumps(record, indent=2, ensure_ascii=False))
    (folder / "classification.json").write_text(json.dumps(classification or {}, indent=2))
    (folder / "extraction.json").write_text(json.dumps(extraction or {}, indent=2, ensure_ascii=False))
    return record


def load_uploads():
    """{email_id: (email, classification, extraction)} for every uploaded email."""
    out = {}
    if not UPLOADS_DIR.exists():
        return out
    for folder in sorted(UPLOADS_DIR.iterdir()):
        try:
            email = json.loads((folder / "email.json").read_text())
            c = json.loads((folder / "classification.json").read_text())
            x = json.loads((folder / "extraction.json").read_text()) or None
            out[email["email_id"]] = (email, c, x)
        except Exception:
            continue                                  # a half-written upload: skip it
    return out


def read_upload_file(path):
    """Bytes of an uploaded attachment, given its 'uploads/<email_id>/<name>' path."""
    _, eid, name = path.split("/", 2)
    return (UPLOADS_DIR / eid / "files" / name).read_bytes()


def delete_upload(eid):
    shutil.rmtree(UPLOADS_DIR / eid, ignore_errors=True)