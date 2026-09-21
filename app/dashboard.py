"""
dashboard.py - CargoCheck dashboard.

    streamlit run app/dashboard.py        (run from the CargoCheck folder)

Pages
  Inbox            every email, its type and result; click one for the SI vs BL view
  Check documents  live run: paste an email to classify it, upload an SI + BL to check them
  Review queue  cases the system could not decide; a person confirms or corrects
  Scores        self-evaluation history from score_log.csv

Reads what pipeline.py saved in output/ (classifications.json and one file per
email in extractions/) plus output/reviews.json. Every result comes from the
pipeline's own decide_email(), so the page matches submission.json exactly; a
person's review is then layered on top (and never changes submission.json).
"""

import html
import json
import os
import re
import sys
from pathlib import Path

from urllib.parse import quote

import altair as alt
import pandas as pd
import streamlit as st

APP_DIR = Path(__file__).resolve().parent
BASE_DIR = APP_DIR.parent
sys.path.insert(0, str(APP_DIR))

DATA_DIR = os.environ.get("DATA_DIR", str(BASE_DIR / "data" / "sdoc-hackathon-bundle"))
sys.path.insert(0, DATA_DIR)

from loader import Inbox                                                # noqa: E402
from comparer import compare_email, FIELDS                              # noqa: E402
from reviews import load_reviews, save_review, delete_review, apply_review  # noqa: E402
from pipeline import (to_comparer_input, CLASSIFICATIONS_FILE,          # noqa: E402
                      EXTRACTIONS_DIR, SCORE_LOG, categorize, decide_email,
                      has_attachments, scanned_files)
from normalizer import normalize_party                                  # noqa: E402
from doc_reader import SmartInbox                                       # noqa: E402
from rule_extractor import extract_email as rule_extract_email, is_confident  # noqa: E402

CATEGORIES = ["BL_COMPARISON", "SI_REQUEST", "INVOICE_QUERY", "GENERAL", "SPAM"]
CATEGORY_LABELS = {"BL_COMPARISON": "BL check", "SI_REQUEST": "New SI request",
                   "INVOICE_QUERY": "Invoice query", "GENERAL": "General", "SPAM": "Spam"}
STATUS_LABELS = {"OK": "No mismatch", "MISMATCH": "Mismatch", "NEEDS_REVIEW": "Needs review",
                 "NOT_CHECKED": "No check needed", "PENDING": "Not processed yet",
                 "NO_DOCUMENTS": "Draft BL requested", "PROCESSING_ERROR": "Processing failed",
                 "RESOLVED": "Resolved by a person"}
# Statuses that stay open until a person reviews them
OPEN_STATUSES = {"NEEDS_REVIEW", "PROCESSING_ERROR"}
REASON_LABELS = {"missing_attachment": "The SI or the BL is not attached.",
                 "unreadable": "A document could not be read.",
                 "wrong_doc_type": "An attachment is not an SI or a BL.",
                 "missing_value": "A required value could not be found."}
FIELD_LABELS = {"shipper": "Shipper", "consignee": "Consignee", "notify_party": "Notify party",
                "port_of_loading": "Port of loading", "port_of_discharge": "Port of discharge",
                "container_count": "Containers", "gross_weight_kg": "Gross weight (kg)"}
PAGES = ["Overview", "Inbox", "Check documents", "Review queue", "Scores"]
PALETTE = {"ink": "#1C2B36", "mismatch": "#D9480F", "match": "#2B7A6B", "review": "#B7791F",
           "muted": "#5B6B77", "line": "#D5DDE2", "yellow": "#F9C74F"}
# Assumptions for the time-saved estimate (shown on the page, adjustable there)
MINUTES_PER_CHECK = 5.0      # comparing one SI with one draft BL by hand
MINUTES_PER_SORT = 0.5       # reading one email and deciding what it needs

st.set_page_config(page_title="CargoCheck", page_icon="⚓", layout="wide")

# --- Look and feel -----------------------------------------------------------
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&family=Archivo:wght@600;800;900&family=Instrument+Serif:ital@0;1&display=swap');
html, body, [class*="css"], .stMarkdown, button, input, textarea, select {
  font-family: 'IBM Plex Sans', system-ui, sans-serif;
}
:root { --ink:#1C2B36; --steel:#F4F6F7; --line:#D5DDE2; --muted:#5B6B77;
        --mismatch:#D9480F; --match:#2B7A6B; --review:#B7791F; }
h1, h2, h3 { color: var(--ink); letter-spacing: -0.01em; }
.cc-lede { color: var(--muted); max-width: 70ch; margin: -0.4rem 0 1.2rem; }
.cc-banner { border-left: 5px solid var(--line); background: #fff; padding: .8rem 1rem;
             margin: .6rem 0 1rem; border-radius: 2px; }
.cc-banner.mismatch { border-color: var(--mismatch); }
.cc-banner.ok { border-color: var(--match); }
.cc-banner.review { border-color: var(--review); }
.cc-banner strong { display:block; font-size:1.05rem; margin-bottom:.15rem; }
.cc-compare { width:100%; border-collapse:collapse; background:#fff; margin-bottom:1rem; }
.cc-compare th { text-align:left; font-weight:600; font-size:.9rem; color:var(--muted);
                 padding:.55rem .8rem; border-bottom:2px solid var(--ink); }
.cc-compare td { padding:.6rem .8rem; border-bottom:1px solid var(--line); vertical-align:top; }
.cc-compare td.field { width:20%; color:var(--muted); font-weight:500; }
.cc-compare tr.mismatch td { background:#FDF1EA; }
.cc-compare tr.mismatch td.field { box-shadow: inset 5px 0 0 var(--mismatch); color:var(--mismatch);
                                   font-weight:600; }
.cc-compare tr.missing td.field { box-shadow: inset 5px 0 0 var(--review); }
.cc-compare .none { color: var(--review); font-style: italic; }
.cc-meta { color: var(--muted); font-size: .92rem; }
.cc-table-wrap { overflow-x: auto; }

/* ---- personality: headlines, ticker, stat cards (working screens stay calm) ---- */
.cc-kicker { font-family: 'Archivo', sans-serif; font-weight: 800; font-size: .78rem;
             letter-spacing: .16em; text-transform: uppercase; color: var(--muted);
             margin: 0 0 .35rem; }
.cc-headline { font-family: 'Archivo', system-ui, sans-serif; font-weight: 900;
               font-size: clamp(2.1rem, 5.2vw, 3.6rem); line-height: 1.02;
               letter-spacing: -0.025em; color: var(--ink); margin: 0 0 .9rem; }
.cc-headline em { font-family: 'Instrument Serif', Georgia, serif; font-style: italic;
                  font-weight: 400; letter-spacing: -0.01em; }
.cc-headline .hl { background: linear-gradient(transparent 62%, #F9C74F 62%); padding: 0 .08em; }
.cc-ticker { overflow: hidden; white-space: nowrap; border-top: 2px solid var(--ink);
             border-bottom: 2px solid var(--ink); margin: .2rem 0 1.4rem; padding: .45rem 0;
             font-family: 'Archivo', sans-serif; font-weight: 800; font-size: .95rem;
             text-transform: uppercase; letter-spacing: .04em; }
.cc-ticker-track { display: inline-block; animation: cc-scroll 38s linear infinite; }
.cc-ticker:hover .cc-ticker-track { animation-play-state: paused; }
.cc-ticker span { margin-right: 2.2rem; }
.cc-ticker em { font-family: 'Instrument Serif', Georgia, serif; font-style: italic; font-weight: 400;
                text-transform: none; font-size: 1.1rem; letter-spacing: 0; }
.cc-ticker .dot { color: var(--mismatch); margin-right: 2.2rem; }
@keyframes cc-scroll { from { transform: translateX(0); } to { transform: translateX(-50%); } }
@media (prefers-reduced-motion: reduce) { .cc-ticker-track { animation: none; } }

.cc-stats { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: .9rem;
            margin: 0 0 1.6rem; }
.cc-stat { background: #fff; border: 2px solid var(--ink); box-shadow: 5px 5px 0 var(--ink);
           padding: .9rem 1rem 1rem; min-height: 7.2rem; position: relative;
           background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='120' height='120'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='.9' numOctaves='2'/%3E%3CfeColorMatrix values='0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 .05 0'/%3E%3C/filter%3E%3Crect width='120' height='120' filter='url(%23n)'/%3E%3C/svg%3E"); }
.cc-stat .num { font-family: 'Archivo', sans-serif; font-weight: 900; font-size: 2.7rem;
                line-height: 1; letter-spacing: -0.03em; }
.cc-stat .lbl { margin-top: .45rem; font-size: .92rem; line-height: 1.3; }
.cc-stat .lbl em { font-family: 'Instrument Serif', Georgia, serif; font-size: 1.1rem; }
.cc-stat.mismatch { background-color: var(--mismatch); color: #fff; transform: rotate(-1.2deg); }
.cc-stat.review { background-color: #FBE3B0; }
.cc-stat.ok { background-color: #D8EDE8; }
@media (max-width: 900px) { .cc-stats { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
@media (max-width: 640px) { .cc-stat .num { font-size: 2.2rem; } .cc-stat { min-height: 6rem; } }

/* Sidebar navigation: page names as a simple menu instead of radio buttons */
[data-testid="stSidebar"] [data-testid="stElementContainer"]:has([data-testid="stRadio"]),
[data-testid="stSidebar"] [data-testid="stRadio"],
[data-testid="stSidebar"] [data-testid="stRadio"] > div { width: 100% !important; }
[data-testid="stSidebar"] [data-testid="stRadioGroup"] { gap: .1rem; width: 100%; }
[data-testid="stSidebar"] [data-testid="stRadioGroup"] > div { width: 100%; }
[data-testid="stSidebar"] [data-testid="stRadioOption"] {
  width: 100%; padding: .5rem .75rem; border-radius: 6px; cursor: pointer;
  transition: background .12s; }
[data-testid="stSidebar"] [data-testid="stRadioOption"] > div > div:first-child { display: none; }
[data-testid="stSidebar"] [data-testid="stRadioOption"] p {
  font-size: 1.05rem; font-weight: 400; color: var(--muted); }
[data-testid="stSidebar"] [data-testid="stRadioOption"]:hover { background: rgba(28,43,54,.05); }
[data-testid="stSidebar"] [data-testid="stRadioOption"]:hover p { color: var(--ink); }
[data-testid="stSidebar"] [data-testid="stRadioOption"][data-selected="true"] {
  background: rgba(28,43,54,.08); }
[data-testid="stSidebar"] [data-testid="stRadioOption"][data-selected="true"] p {
  color: var(--ink); font-weight: 600; }
[data-testid="stSidebar"] [data-testid="stRadioOption"][data-focus-visible="true"] {
  outline: 2px solid var(--ink); outline-offset: 2px; }
  
[data-testid="stSidebar"] .cc-brand { font-family: 'Archivo', sans-serif; font-weight: 900;
    font-size: 1.6rem; letter-spacing: -0.02em; margin: 0; }
[data-testid="stSidebar"] .cc-brand em { font-family: 'Instrument Serif', Georgia, serif;
    font-weight: 400; font-style: italic; }
/* ---- "How it works" strip for first-time users ---- */
.cc-steps { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: .8rem;
            margin: 0 0 .6rem; }
.cc-step { background: #fff; border: 1px solid var(--line); border-top: 4px solid var(--ink);
           padding: .8rem 1rem; }
.cc-step .n { font-family: 'Archivo', sans-serif; font-weight: 900; font-size: 1.5rem;
              line-height: 1; color: var(--muted); }
.cc-step strong { display: block; margin: .3rem 0 .2rem; font-size: 1.02rem; }
.cc-step p { margin: 0; color: var(--muted); font-size: .92rem; line-height: 1.4; }
.cc-banner ul { margin: .35rem 0 0 1.1rem; padding: 0; }
.cc-banner .what { display: block; margin-top: .4rem; }
@media (max-width: 760px) { .cc-steps { grid-template-columns: 1fr; } }
.cc-insight { font-size: .95rem; color: var(--ink); margin: -.3rem 0 1.2rem;
              border-left: 3px solid var(--yellow, #F9C74F); padding-left: .6rem; }
.cc-section { font-family: 'Archivo', sans-serif; font-weight: 800; font-size: 1.05rem;
              margin: .4rem 0 .2rem; color: var(--ink); }
/* long attachment text wraps instead of running off the screen */
[data-testid="stText"] pre, [data-testid="stText"] { white-space: pre-wrap !important;
                                                    word-break: break-word; }
/* comfortable touch targets on phones and tablets */
@media (pointer: coarse) {
  .stButton button, [data-baseweb="select"] > div { min-height: 44px; }
}
/* phones: each field becomes a small card, SI and BL stacked */
@media (max-width: 640px) {
  .block-container { padding-left: 1rem; padding-right: 1rem; }
  h1 { font-size: 1.9rem; }
  .cc-compare thead { display: none; }
  .cc-compare, .cc-compare tbody, .cc-compare tr, .cc-compare td { display: block; width: 100%; }
  .cc-compare tr { border-bottom: 1px solid var(--line); padding: .55rem 0 .6rem; }
  .cc-compare td { border: none; padding: .1rem .9rem; }
  .cc-compare td.field { width: auto; color: var(--ink); font-weight: 600; padding-bottom: .3rem; }
  .cc-compare td[data-label]::before { content: attr(data-label); display: block;
                                       font-size: .78rem; color: var(--muted); }
  .cc-compare td[data-label] { padding-bottom: .35rem; }
  .cc-compare tr.mismatch td.field, .cc-compare tr.missing td.field { box-shadow: none; }
  .cc-compare tr.mismatch { box-shadow: inset 5px 0 0 var(--mismatch); background: #FDF1EA; }
  .cc-compare tr.missing { box-shadow: inset 5px 0 0 var(--review); }
  .cc-compare tr.mismatch td { background: transparent; }
}
</style>
""", unsafe_allow_html=True)


# --- Data --------------------------------------------------------------------

@st.cache_data
def load_emails():
    return {e["email_id"]: e for e in Inbox(DATA_DIR)}


def load_json(path):
    return json.loads(path.read_text()) if path.exists() else {}


def load_extraction(eid):
    return load_json(EXTRACTIONS_DIR / f"{eid}.json") or None


def base_input(eid, category, classification, extraction, rule_note=None):
    """What the AI and rules said, before any person changed anything."""
    if category == "BL_COMPARISON" and extraction and "processing_error" not in extraction:
        data = to_comparer_input(eid, category, extraction)
        data["issue_notes"] = extraction.get("extraction_issues", [])
        data["method"] = "AI" if extraction.get("method") == "llm" else "rules"
    else:
        data = {"email_id": eid, "category": category}
    data["classifier_confidence"] = classification.get("confidence")
    data["classifier_reason"] = rule_note or classification.get("reason")
    data["classifier_needs_review"] = classification.get("needs_review", False)
    return data


def reviewed_result(eid, email, data, review, original_category, extraction, entry, report):
    """Recompute the result after a person's review."""
    category = data.get("category")
    if category != "BL_COMPARISON":
        return decide_email(eid, email, category, None)          # re-sorted: no check needed
    if review.get("si") or review.get("bl"):
        # the person typed values: compare exactly what they entered
        return compare_email(dict(data, email_id=eid, category="BL_COMPARISON", issues=[]))
    if category != original_category:
        return decide_email(eid, email, category, extraction)    # re-sorted into a BL check
    return entry, report                                          # confirmed as is


def build_results():
    """For every email: the merged AI+review data, submission entry and report."""
    emails, reviews = load_emails(), load_reviews()
    classifications = load_json(CLASSIFICATIONS_FILE)
    categories, rule_notes = categorize(emails, classifications)
    results = {}
    for eid, email in emails.items():
        c = classifications.get(eid, {})
        review = reviews.get(eid)
        extraction = load_extraction(eid)
        category = categories[eid]
        base = base_input(eid, category, c, extraction, rule_notes.get(eid))
        not_extracted = (category == "BL_COMPARISON" and has_attachments(email)
                         and extraction is None)
        if not c.get("category") or not_extracted:
            results[eid] = {"email": email, "data": dict(base, category=c.get("category") and category),
                            "base": base, "entry": None, "report": None, "status": "PENDING",
                            "review": review}
            continue

        entry, report = decide_email(eid, email, category, extraction)   # same as submission.json
        data = base
        if review:
            data = apply_review(base, review)
            entry, report = reviewed_result(eid, email, data, review, category, extraction,
                                            entry, report)
        status = report["status"]
        if review and status in OPEN_STATUSES:
            status = "RESOLVED"                   # a person has made the final call
        results[eid] = {"email": email, "data": data, "base": base, "entry": entry,
                        "report": report, "status": status, "review": review}
    return results


def needs_person(r):
    data = r["data"]
    return (r["status"] in OPEN_STATUSES or data.get("classifier_needs_review")
            or data.get("classifier_confidence") == "low")


def read_attachment(path):
    """Text of any attachment. PDF, Word and Excel are decoded; scans use the cached
    vision transcription from the pipeline run."""
    try:
        return SmartInbox(Inbox(DATA_DIR), DATA_DIR).read_text(path)
    except Exception as e:
        return f"Could not open this file: {e}"


def page_heading(kicker, headline_html):
    st.markdown(f'<p class="cc-kicker">{kicker}</p><h1 class="cc-headline">{headline_html}</h1>',
                unsafe_allow_html=True)


def ticker(items):
    """A scrolling strip of live numbers. Duplicated once so the loop is seamless."""
    parts = "".join(f"<span>{i}</span><span class='dot'>●</span>" for i in items)
    plain = re.sub(r"<[^>]+>", "", " · ".join(items))
    st.markdown(f'<div class="cc-ticker" role="marquee" aria-label="{html.escape(plain)}">'
                f'<div class="cc-ticker-track">{parts}{parts}</div></div>',
                unsafe_allow_html=True)


def plural(n, word):
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def cell(value):
    if value is None or str(value).strip() == "":
        return '<span class="none">Not found</span>'
    return html.escape(str(value))


# --- Shared pieces -----------------------------------------------------------

REVIEW_ACTIONS = {
    "missing_attachment": "Ask the sender to resend the missing SI or draft BL.",
    "unreadable": ("Open the attachment below, compare it with the values the system read, "
                   "then confirm them or type in the right ones."),
    "wrong_doc_type": ("Ask the sender for the correct document, or confirm if this "
                       "attachment is expected."),
    "missing_value": ("Look at the attachment below and type the missing value in, "
                      "or confirm it really is missing."),
}


def result_banner(r):
    """Says what the result means AND what the person should do next."""
    report = r["report"]
    rows = report.get("fields") or []
    if r["status"] == "MISMATCH":
        fixes = "".join(
            f"<li><b>{FIELD_LABELS[row['field']]}</b> should be <b>{cell(row['si'])}</b> "
            f"(the draft BL says {cell(row['bl'])})</li>"
            for row in rows if row["result"] == "MISMATCH")
        title, kind = "Ask for the draft BL to be amended", "mismatch"
        body = f"The draft BL doesn't match the Shipping Instruction:<ul>{fixes}</ul>"
    elif r["status"] == "OK":
        title, kind = "No mismatch detected", "ok"
        body = "The draft BL matches the Shipping Instruction on all 7 fields. It's safe to confirm."
    elif r["status"] == "RESOLVED":
        rv = r["review"]
        verb = "Confirmed" if rv.get("decision") == "confirmed" else "Corrected"
        title, kind = "Resolved by a person", "ok"
        body = f"{verb} on {html.escape(rv.get('reviewed_at', ''))}."
        if rv.get("note"):
            body += f" Note: {html.escape(rv['note'])}"
        reason = report.get("reason") or r["entry"].get("review_reason")
        if reason in REASON_LABELS:
            body += f" Originally flagged because: {REASON_LABELS[reason].lower()}"
    elif r["status"] == "NO_DOCUMENTS":
        title, kind = "Nothing to compare yet", ""
        body = ("The sender is asking for the draft BL. Once the SI and draft BL arrive, "
                "they can be checked here.")
    elif r["status"] == "PROCESSING_ERROR":
        title, kind = "Processing failed", "review"
        body = (html.escape(report.get("detail", "")) +
                '<span class="what"><b>What to do:</b> rerun <code>python app/pipeline.py</code> '
                "to retry this email.</span>")
    else:
        reason = report.get("reason") or r["entry"].get("review_reason")
        title, kind = "A person needs to check this", "review"
        body = REASON_LABELS.get(reason, html.escape(report.get("detail", "")))
        missing = [FIELD_LABELS[row["field"]].lower() for row in rows if row["result"] == "MISSING"]
        if reason == "missing_value" and missing:
            body += f" Missing: {', '.join(missing)}."
        notes = r["data"].get("issue_notes") or []
        if notes and reason != "missing_value":
            body += " Details: " + "; ".join(html.escape(n) for n in notes)
        if reason in REVIEW_ACTIONS:
            body += f'<span class="what"><b>What to do:</b> {REVIEW_ACTIONS[reason]}</span>'
    st.markdown(f'<div class="cc-banner {kind}"><strong>{title}</strong>{body}</div>',
                unsafe_allow_html=True)


def comparison_table(r):
    rows = "".join(
        f'<tr class="{row["result"].lower()}"><td class="field">{FIELD_LABELS[row["field"]]}</td>'
        f'<td data-label="Shipping Instruction">{cell(row["si"])}</td>'
        f'<td data-label="Draft Bill of Lading">{cell(row["bl"])}</td></tr>'
        for row in r["report"]["fields"])
    st.markdown(
        '<div class="cc-table-wrap"><table class="cc-compare"><thead><tr><th>Field</th>'
        '<th>Shipping Instruction</th><th>Draft Bill of Lading</th></tr></thead>'
        f'<tbody>{rows}</tbody></table></div>', unsafe_allow_html=True)


def source_documents(email):
    for path in email.get("attachments") or []:
        with st.expander(f"Attachment: {Path(path).name}"):
            text = read_attachment(path)
            if text is None:
                st.caption("This is a PDF, Word or Excel file. Open it from the data folder to view it.")
            else:
                st.text(text)


def amendment_email(r, to="", subject=""):
    """A ready-to-send email asking for the draft BL to be amended."""
    fixes = [row for row in (r["report"].get("fields") or []) if row["result"] == "MISMATCH"]
    lines = [f'{i}. {FIELD_LABELS[row["field"]]}: should read "{row["si"]}" '
             f'(the draft BL currently shows "{row["bl"]}")'
             for i, row in enumerate(fixes, 1)]
    subject = f"RE: {subject}" if subject else "Draft BL amendment request"
    body = ("Dear team,\n\n"
            "Thank you for the draft Bill of Lading. We have checked it against our Shipping "
            "Instruction and found the following difference" + ("s" if len(lines) > 1 else "") +
            ":\n\n" + "\n".join(lines) +
            "\n\nKindly amend the draft BL accordingly and send the revised draft for our "
            "confirmation.\n\nBest regards,\n[Your name]")
    return to, subject, body


def amendment_email_box(r, to="", subject=""):
    """Shows the draft email with a copy button and an 'open in email app' link."""
    to, subject, body = amendment_email(r, to, subject)
    with st.expander("✉️  Draft the amendment email", expanded=False):
        st.caption("Ready to send: copy it with the button in the corner of the box, "
                   "or open it straight in your email app.")
        st.markdown(f"**To:** {html.escape(to) or '(sender)'}  \n**Subject:** {html.escape(subject)}")
        st.code(body, language=None, wrap_lines=True)
        mailto = f"mailto:{quote(to)}?subject={quote(subject)}&body={quote(body)}"
        st.link_button("Open in my email app", mailto)


def go_to_review(eid):
    st.session_state.page = "Review queue"
    st.session_state.review_target = eid


# --- Page: Overview ----------------------------------------------------------

def bar_chart(df, x, y, color, height=None):
    """Horizontal bars, biggest first, in the dashboard's colours."""
    return (alt.Chart(df)
            .mark_bar(color=color, cornerRadiusEnd=2)
            .encode(x=alt.X(f"{x}:Q", title=None, axis=alt.Axis(tickMinStep=1, grid=False)),
                    y=alt.Y(f"{y}:N", sort="-x", title=None,
                            axis=alt.Axis(labelLimit=260, labelFontSize=12)),
                    tooltip=list(df.columns))
            .properties(height=height or max(140, 34 * len(df))))


def donut(df, label, value, colors):
    return (alt.Chart(df)
            .mark_arc(innerRadius=62, outerRadius=110, stroke="#fff", strokeWidth=2)
            .encode(theta=alt.Theta(f"{value}:Q"),
                    color=alt.Color(f"{label}:N", title=None,
                                    scale=alt.Scale(domain=list(df[label]), range=colors),
                                    legend=alt.Legend(orient="right", labelFontSize=12)),
                    tooltip=[label, value])
            .properties(height=240))


def insight(text):
    st.markdown(f'<p class="cc-insight">{text}</p>', unsafe_allow_html=True)


def page_overview(results):
    page_heading("Overview", 'What the inbox is <em>telling</em> you <span class="hl">today</span>')

    processed = [r for r in results.values() if r["status"] != "PENDING"]
    checks = [r for r in processed if r["data"].get("category") == "BL_COMPARISON"]
    compared = [r for r in checks if r["status"] not in ("NO_DOCUMENTS", "PROCESSING_ERROR")]
    mismatches = [r for r in checks if r["status"] == "MISMATCH"]
    waiting = [r for r in checks if r["status"] in OPEN_STATUSES]

    # how each compared email's documents were read
    def read_by(r):
        if scanned_files(r["email"]):
            return "AI vision (scans)"
        return "AI (LLM)" if r["data"].get("method") == "AI" else "Rules (no AI)"
    methods = pd.Series([read_by(r) for r in compared], dtype="object").value_counts()
    rules_share = methods.get("Rules (no AI)", 0) / max(len(compared), 1)

    with st.expander("How the time saved is estimated"):
        c1, c2 = st.columns(2)
        per_check = c1.number_input("Minutes to compare one SI and BL by hand", 1.0, 60.0,
                                    MINUTES_PER_CHECK, 0.5)
        per_sort = c2.number_input("Minutes to read and sort one email", 0.1, 10.0,
                                   MINUTES_PER_SORT, 0.1)
    hours = (len(compared) * per_check + len(processed) * per_sort) / 60

    st.markdown(f"""
<div class="cc-stats">
  <div class="cc-stat mismatch"><div class="num">{len(mismatches)}</div>
       <div class="lbl">draft BLs with <em>errors</em> caught before finalising</div></div>
  <div class="cc-stat review"><div class="num">{len(waiting)}</div>
       <div class="lbl">cases waiting for <em>a person</em></div></div>
  <div class="cc-stat ok"><div class="num">{rules_share:.0%}</div>
       <div class="lbl">of documents read by <em>free rules</em>, no AI needed</div></div>
  <div class="cc-stat"><div class="num">~{hours:.0f}h</div>
       <div class="lbl">of manual checking <em>saved</em> on {len(processed)} emails</div></div>
</div>""", unsafe_allow_html=True)

    left, right = st.columns(2, gap="large")

    # 1. which fields go wrong most often
    with left:
        st.markdown('<p class="cc-section">What goes wrong most often</p>', unsafe_allow_html=True)
        field_counts = pd.Series([f for r in mismatches for f in r["entry"]["defect_fields"]],
                                 dtype="object").value_counts()
        if len(field_counts):
            df = pd.DataFrame({"Field": [FIELD_LABELS[f] for f in field_counts.index],
                               "Mismatched BLs": field_counts.values})
            st.altair_chart(bar_chart(df, "Mismatched BLs", "Field", PALETTE["mismatch"]),
                            width="stretch")
            top = df.iloc[0]
            insight(f"<b>{top['Field']}</b> is the most common error: wrong on "
                    f"{top['Mismatched BLs']} of {len(mismatches)} draft BLs with mismatches.")
        else:
            st.write("No mismatches found yet.")

    # 2. inbox breakdown
    with right:
        st.markdown('<p class="cc-section">Inbox at a glance</p>', unsafe_allow_html=True)
        cats = pd.Series([r["data"].get("category") for r in processed],
                         dtype="object").value_counts()
        df = pd.DataFrame({"Type": [CATEGORY_LABELS.get(c, c) for c in cats.index],
                           "Emails": cats.values})
        st.altair_chart(donut(df, "Type", "Emails",
                              [PALETTE["ink"], PALETTE["match"], PALETTE["yellow"],
                               PALETTE["muted"], PALETTE["line"]]), width="stretch")
        if len(df):
            insight(f"<b>{len(checks)}</b> of {len(processed)} emails ask for a BL check; "
                    f"the rest only needed sorting.")

    left, right = st.columns(2, gap="large")

    # 3. which shippers' BLs have the most mismatches
    with left:
        st.markdown('<p class="cc-section">Shippers with the most mismatched BLs</p>',
                    unsafe_allow_html=True)
        names, per_shipper = {}, {}
        for r in compared:
            raw = next((row["si"] for row in r["report"].get("fields", [])
                        if row["field"] == "shipper" and row["si"]), None)
            if not raw:
                continue
            key = normalize_party(raw)
            names.setdefault(key, str(raw).split(" ON BEHALF OF")[0][:40])
            total, bad = per_shipper.get(key, (0, 0))
            per_shipper[key] = (total + 1, bad + (r["status"] == "MISMATCH"))
        rows = [{"Shipper": names[k], "Mismatched BLs": bad, "BL checks": total,
                 "Error rate": f"{bad / total:.0%}"}
                for k, (total, bad) in per_shipper.items() if bad]
        if rows:
            df = pd.DataFrame(rows).sort_values("Mismatched BLs", ascending=False).head(6)
            st.altair_chart(bar_chart(df, "Mismatched BLs", "Shipper", PALETTE["ink"]),
                            width="stretch")
            top = df.iloc[0]
            insight(f"<b>{top['Shipper']}</b> has the most mismatched BLs "
                    f"({top['Mismatched BLs']} of {top['BL checks']} checks, {top['Error rate']}).")
        else:
            st.write("No mismatches found yet.")

    # 4. how documents were read
    with right:
        st.markdown('<p class="cc-section">How the documents were read</p>',
                    unsafe_allow_html=True)
        if len(methods):
            order = ["Rules (no AI)", "AI (LLM)", "AI vision (scans)"]
            df = pd.DataFrame({"Method": [m for m in order if m in methods],
                               "Emails": [int(methods[m]) for m in order if m in methods]})
            st.altair_chart(donut(df, "Method", "Emails",
                                  [PALETTE["match"], PALETTE["ink"], PALETTE["review"]]),
                            width="stretch")
            insight(f"<b>{rules_share:.0%}</b> of documents were read by free, instant rules. "
                    "The AI is only used where the rules can't cope, and scans always go "
                    "to a person to confirm.")
        else:
            st.write("No documents read yet.")


# --- Page: Inbox -------------------------------------------------------------

def email_detail(r):
    email, data = r["email"], r["data"]
    st.subheader(email.get("subject") or "(no subject)")
    category = data.get("category")
    meta = f'{html.escape(email.get("from", ""))} &nbsp;|&nbsp; {email["email_id"]}'
    if category:
        meta += f' &nbsp;|&nbsp; {CATEGORY_LABELS[category]}'
    st.markdown(f'<p class="cc-meta">{meta}</p>', unsafe_allow_html=True)
    if data.get("classifier_reason"):
        st.caption(f"Why this type: {data['classifier_reason']}")
    if data.get("method"):
        st.caption(f"Documents read by: {data['method']}")

    if r["status"] == "PENDING":
        st.info("This email has not been processed yet. Run `python app/pipeline.py` to process it.")
    elif category == "BL_COMPARISON" and r["report"]:
        result_banner(r)
        if r["report"]["fields"]:
            comparison_table(r)
        if r["status"] == "MISMATCH":
            amendment_email_box(r, to=email.get("from", ""), subject=email.get("subject", ""))
    else:
        st.write("This type of email only needs sorting, not a document check.")

    if r["review"]:
        rv = r["review"]
        st.caption(f"Checked by a person on {rv.get('reviewed_at')}: {rv.get('note') or 'no note'}")
    if r["status"] != "PENDING":
        urgent = r["status"] in OPEN_STATUSES
        st.button("Review this email" if urgent else "Check or correct this result",
                  on_click=go_to_review, args=(email["email_id"],),
                  type="primary" if urgent else "secondary", key=f"goto_{email['email_id']}")

    with st.expander("Email text"):
        st.text(email.get("body", ""))
    source_documents(email)


def how_it_works():
    """Three plain-language steps for first-time users. Can be hidden."""
    if st.session_state.get("hide_intro"):
        return
    st.markdown("""
<div class="cc-steps">
  <div class="cc-step"><div class="n">1</div><strong>Sort every email</strong>
    <p>Each email is sorted into BL checks, new SI requests, invoice queries, general
    updates or spam.</p></div>
  <div class="cc-step"><div class="n">2</div><strong>Compare the SI and draft BL</strong>
    <p>For BL checks, 7 fields on the draft Bill of Lading are compared with the Shipping
    Instruction, and every difference is flagged.</p></div>
  <div class="cc-step"><div class="n">3</div><strong>Ask a person when unsure</strong>
    <p>Missing files, unreadable scans or missing values go to the review queue, with the
    evidence, instead of being guessed.</p></div>
</div>""", unsafe_allow_html=True)
    if st.button("Got it, hide this", key="hide_intro_btn"):
        st.session_state.hide_intro = True
        st.rerun()


INBOX_TABS = [
    ("needs", "Needs a person", {"NEEDS_REVIEW", "PROCESSING_ERROR", "PENDING"},
     "Start here. The system couldn't decide these on its own."),
    ("mismatch", "Mismatches to fix", {"MISMATCH"},
     "The draft BL differs from the SI. Ask for the BL to be amended."),
    ("done", "Clean and resolved", {"OK", "RESOLVED"},
     "Nothing to do: the BL matches the SI, or a person has already decided."),
    ("other", "Other emails", {"NOT_CHECKED", "NO_DOCUMENTS"},
     "Sorted only: new SI requests, invoice queries, general updates, spam, and requests "
     "for a draft BL that has not arrived yet."),
]
URGENCY = {"PROCESSING_ERROR": 0, "NEEDS_REVIEW": 1, "PENDING": 2, "MISMATCH": 3}


def inbox_table(key, items, results):
    """One tab's table. Selecting a row shows that email's details underneath."""
    rows = []
    for eid, r in items:
        fields = r["entry"]["defect_fields"] if r["entry"] else []
        result = STATUS_LABELS[r["status"]]
        if r["review"] and r["status"] != "RESOLVED":
            result += " · checked by a person"
        rows.append({"Email": eid, "Result": result,
                     "Fields to fix": ", ".join(FIELD_LABELS[f] for f in fields),
                     "Type": CATEGORY_LABELS.get(r["data"].get("category"), "Not processed"),
                     "Subject": r["email"].get("subject", "")})
    table = pd.DataFrame(rows)
    picked = st.dataframe(table, hide_index=True, width="stretch", height=320,
                          on_select="rerun", selection_mode="single-row", key=f"table_{key}",
                          column_config={"Subject": st.column_config.TextColumn(width="large")})
    selected = picked.selection.rows if picked and picked.selection else []
    if selected:
        st.divider()
        email_detail(results[table.iloc[selected[0]]["Email"]])
    else:
        st.caption("Click a row to see the email and, for BL checks, the SI and BL side by side.")


def page_inbox(results):
    checks = [r for r in results.values() if r["data"].get("category") == "BL_COMPARISON"]
    count = lambda s: sum(r["status"] == s for r in checks)                      # noqa: E731
    others = sum(1 for r in results.values()
                 if r["data"].get("category") not in (None, "BL_COMPARISON"))

    page_heading("Inbox", 'Every BL, <em>checked</em> against its <span class="hl">SI</span>')
    how_it_works()
    ticker([f"{len(results)} emails in", f"{len(checks)} <em>BL checks</em>",
            f"{plural(count('MISMATCH'), 'mismatch').replace('mismatchs', 'mismatches')} caught",
            f"{count('NEEDS_REVIEW') + count('PROCESSING_ERROR')} <em>waiting for a person</em>",
            f"{count('RESOLVED')} resolved by a person",
            f"{count('OK')} clean", "SI vs draft BL, field by field"])
    st.markdown(f"""
<div class="cc-stats">
  <div class="cc-stat mismatch"><div class="num">{count('MISMATCH')}</div>
       <div class="lbl">BLs with <em>mismatches</em> to fix</div></div>
  <div class="cc-stat review"><div class="num">{count('NEEDS_REVIEW') + count('PROCESSING_ERROR')}</div>
       <div class="lbl">waiting for <em>a person</em></div></div>
  <div class="cc-stat ok"><div class="num">{count('OK')}</div>
       <div class="lbl">clean, <em>no mismatch</em></div></div>
  <div class="cc-stat"><div class="num">{others}</div>
       <div class="lbl">other emails, <em>sorted</em></div></div>
</div>""", unsafe_allow_html=True)

    search = st.text_input("Search", placeholder="Search by subject or email ID, e.g. email_004",
                           label_visibility="collapsed")
    matching = [(eid, r) for eid, r in results.items()
                if not search or search.lower() in (r["email"].get("subject", "") + eid).lower()]

    groups = {key: sorted((x for x in matching if x[1]["status"] in statuses),
                          key=lambda x: (URGENCY.get(x[1]["status"], 9), x[0]))
              for key, _, statuses, _ in INBOX_TABS}
    tabs = st.tabs([f"{label} ({len(groups[key])})" for key, label, _, _ in INBOX_TABS])
    for tab, (key, label, _, hint) in zip(tabs, INBOX_TABS):
        with tab:
            st.caption(hint)
            if groups[key]:
                inbox_table(key, groups[key], results)
            elif search:
                st.write("No emails in this tab match your search.")
            else:
                st.write("Nothing here right now.")


# --- Page: Check documents (live run) -----------------------------------------

class UploadedFiles:
    """Lets the extractors read uploaded files the same way they read the inbox."""
    def __init__(self, files):
        self.files = files                                   # {path: bytes}

    def read_bytes(self, path):
        return self.files[path]

    def read_text(self, path, encoding="utf-8"):
        return self.files[path].decode(encoding, errors="replace")


def run_check(si_name, si_bytes, bl_name, bl_bytes, use_ai):
    """Extract (rules first, AI if asked and needed) and compare two uploaded documents.
    Returns the same result shape the Inbox uses, plus how the documents were read."""
    si_path = f"upload_SI{Path(si_name).suffix.lower()}"
    bl_path = f"upload_BL{Path(bl_name).suffix.lower()}"
    source = UploadedFiles({si_path: si_bytes, bl_path: bl_bytes})
    email = {"email_id": "upload", "attachments": [si_path, bl_path]}

    extraction, method, ai_note = rule_extract_email(source, email), "rules", None
    if use_ai and not is_confident(extraction):
        try:
            from extractor import extract_email as llm_extract_email
            extraction, method = llm_extract_email(source, email), "AI"
        except Exception as e:
            ai_note = f"AI extraction unavailable, showing the rules result ({str(e)[:120]})"

    data = to_comparer_input("upload", "BL_COMPARISON", extraction)
    data["issue_notes"] = extraction.get("extraction_issues", [])
    entry, report = compare_email(dict(data))
    return {"data": data, "entry": entry, "report": report, "status": report["status"],
            "method": method, "ai_note": ai_note}


def classify_text(subject, body):
    from classifier import classify_batch
    email = {"email_id": "live", "from": "", "subject": subject, "body": body, "attachments": []}
    return classify_batch([email]).get("live")


def page_check():
    page_heading("Check documents", 'Try it <em>live</em>: <span class="hl">check</span> a BL against its SI')

    c1, c2 = st.columns(2)
    si_file = c1.file_uploader("Shipping Instruction", type=["txt", "pdf", "docx", "xlsx"], key="si_up")
    bl_file = c2.file_uploader("Draft Bill of Lading", type=["txt", "pdf", "docx", "xlsx"], key="bl_up")
    use_ai = st.toggle("Use AI for anything the rules can't read", value=True)

    if st.button("Check these documents", type="primary", disabled=not (si_file and bl_file)):
        with st.spinner("Reading both documents and comparing seven fields..."):
            r = run_check(si_file.name, si_file.getvalue(), bl_file.name, bl_file.getvalue(), use_ai)
        st.caption(f"Documents read by: {r['method']}")
        if r["ai_note"]:
            st.caption(r["ai_note"])
        result_banner(r)
        if r["report"]["fields"]:
            comparison_table(r)
        if r["status"] == "MISMATCH":
            amendment_email_box(r)


# --- Page: Review queue ------------------------------------------------------

def review_form(r):
    eid, data = r["email"]["email_id"], r["data"]
    ai = r["base"]                                # what the system said, before any review

    category = st.selectbox("Email type", CATEGORIES, format_func=CATEGORY_LABELS.get,
                            index=CATEGORIES.index(data.get("category") or "GENERAL"),
                            key=f"cat_{eid}")
    corrected = {"si": {}, "bl": {}}
    if category == "BL_COMPARISON":
        st.markdown("Correct any value below. Leave a box empty if the value really is missing.")
        left, right = st.columns(2)
        for doc, col, title in (("si", left, "Shipping Instruction"), ("bl", right, "Draft Bill of Lading")):
            col.markdown(f"**{title}**")
            current = data.get(doc) or {}
            original = ai.get(doc) or {}
            for f in FIELDS:
                value = current.get(f)
                typed = col.text_input(FIELD_LABELS[f], "" if value is None else str(value),
                                       key=f"{doc}_{f}_{eid}")
                was = original.get(f)
                if typed != ("" if was is None else str(was)):
                    corrected[doc][f] = typed.strip() or None

    note = st.text_area("Note for the team", r["review"].get("note", "") if r["review"] else "",
                        key=f"note_{eid}", placeholder="What did you check, and what did you change?")

    if st.button("Save corrections", type="primary", key=f"save_{eid}"):
        review = {"decision": "corrected", "note": note}
        if category != ai.get("category"):
            review["category"] = category
        for doc in ("si", "bl"):
            if corrected[doc]:
                review[doc] = corrected[doc]
        save_review(eid, review)
        st.session_state.flash = f"Saved corrections for {eid}."
        st.rerun()
    if st.button("Confirm as is", key=f"confirm_{eid}"):
        review = {"decision": "confirmed", "note": note}
        if category != ai.get("category"):
            review["category"] = category
        save_review(eid, review)
        st.session_state.flash = f"Confirmed {eid}."
        st.rerun()
    if r["review"] and st.button("Undo my review", key=f"undo_{eid}"):
        delete_review(eid)
        st.session_state.flash = f"Removed the review for {eid}. The AI result is back."
        st.rerun()


def page_review(results):
    page_heading("Review queue", 'Where a <em>person</em> has the <span class="hl">final say</span>')
    open_items = {eid: r for eid, r in results.items() if needs_person(r) and not r["review"]}
    done = {eid: r for eid, r in results.items() if r["review"]}
    st.markdown(
        f'<p class="cc-lede">{plural(len(open_items), "email")} '
        f'{"needs" if len(open_items) == 1 else "need"} a person to check. '
        f'{len(done)} checked so far. A saved review updates the result everywhere in '
        'this dashboard; the AI result stays underneath and can be restored with Undo.</p>',
        unsafe_allow_html=True)

    if st.session_state.get("flash"):
        st.success(st.session_state.pop("flash"))

    # "Review this email" (from the Inbox) pins that email here. It stays pinned
    # while the person types, because Streamlit reruns the page on every edit.
    target = st.session_state.pop("review_target", None)
    if target:
        st.session_state.review_pick = target
    pinned = st.session_state.get("review_pick")
    options = list(open_items) + [e for e in done if e not in open_items]
    if pinned and pinned in results and pinned not in options:
        options.insert(0, pinned)
    if not options:
        st.write("Nothing to review. Emails the system cannot decide on will appear here.")
        return
    if st.session_state.get("review_pick") not in options:
        st.session_state.review_pick = options[0]

    def label(eid):
        r = results[eid]
        if r["review"]:
            why = "Checked"
        elif r["status"] == "MISMATCH":
            why = "Mismatch, opened from the inbox"
        elif r["status"] == "NEEDS_REVIEW":
            why = REASON_LABELS.get(r["entry"].get("review_reason"), "Needs review").rstrip(".")
        else:
            why = "Unsure about the email type"
        return f"{eid}: {why}"

    eid = st.selectbox("Email", options, format_func=label, key="review_pick")
    r = results[eid]
    st.divider()
    st.subheader(r["email"].get("subject") or "(no subject)")
    if r["data"].get("category") == "BL_COMPARISON" and r["report"]:
        result_banner(r)
        if r["report"]["fields"]:
            comparison_table(r)
    elif r["data"].get("classifier_reason"):
        st.caption(f"Why the system chose {CATEGORY_LABELS.get(r['data'].get('category'))}: "
                   f"{r['data']['classifier_reason']}")

    # Stacked, not side by side: the form's SI/BL columns need the full width
    # to stay readable on tablets and phones.
    st.markdown("**Source evidence**")
    with st.expander("Email text", expanded=False):
        st.text(r["email"].get("body", ""))
    source_documents(r["email"])
    st.markdown("**Your decision**")
    review_form(r)


# --- Page: Scores ------------------------------------------------------------

def page_scores():
    page_heading("Scores", 'How the system <em>got better</em>, run by run')
    if not SCORE_LOG.exists():
        st.write("No scores yet. Run `python app/pipeline.py --submit` to score a submission.")
        return
    log = pd.read_csv(SCORE_LOG)
    latest = log.iloc[-1]
    st.markdown(
        f'<p class="cc-lede">Latest score {latest["final"]:.4f} out of 1, on {latest["time"]}'
        f'{": " + str(latest["note"]) if pd.notna(latest["note"]) and latest["note"] else ""}. '
        'The score is 30% email sorting, 20% mismatch detection and 50% mismatches caught '
        'end to end.</p>', unsafe_allow_html=True)
    if len(log) > 1:
        chart = log.reset_index().rename(columns={"index": "run"})
        chart["run"] += 1
        st.line_chart(chart, x="run", y="final", height=260)
    st.dataframe(log.rename(columns={
        "time": "When", "final": "Final score", "class_f1": "Sorting F1",
        "defect_f1": "Mismatch F1", "e2e": "Caught end to end",
        "escalation_recall": "Escalated correctly", "note": "What changed"}),
        hide_index=True, width="stretch")


# --- Layout ------------------------------------------------------------------

with st.sidebar:
    st.markdown('<p class="cc-brand">Cargo<em>Check</em></p>', unsafe_allow_html=True)
    st.caption("Checks draft Bills of Lading against Shipping Instructions.")
    st.radio("Go to", PAGES, key="page", label_visibility="collapsed")

if not CLASSIFICATIONS_FILE.exists() and st.session_state.page != "Scores":
    st.warning("No results yet. Run `python app/classifier.py` and `python app/pipeline.py` "
               "first, then refresh this page.")

results = build_results()
if st.session_state.page == "Overview":
    page_overview(results)
elif st.session_state.page == "Inbox":
    page_inbox(results)
elif st.session_state.page == "Check documents":
    page_check()
elif st.session_state.page == "Review queue":
    page_review(results)
else:
    page_scores()