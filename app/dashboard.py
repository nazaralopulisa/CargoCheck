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
/* ---- Overview: simple labelled bar lists (no legends, labels wrap) ---- */
/* one grid per list, so every row's bar starts and ends at the same place */
.cc-bars { list-style: none; margin: .3rem 0 .8rem; padding: 0; display: grid;
           grid-template-columns: minmax(7rem, 34%) 1fr auto; align-items: center;
           column-gap: .75rem; row-gap: .5rem; }
.cc-bar-row { display: contents; }
.cc-bar-label { font-size: .92rem; line-height: 1.25; color: var(--ink); }
.cc-bar-track { background: rgba(28,43,54,.06); height: 1.3rem; border-radius: 3px; overflow: hidden; }
.cc-bar-fill { display: block; height: 100%; border-radius: 3px; }
.cc-bar-val { font-weight: 700; font-variant-numeric: tabular-nums; min-width: 2.2rem;
              text-align: right; white-space: nowrap; }
.cc-bar-val small { font-weight: 400; color: var(--muted); font-size: .82rem; margin-left: .25rem; }
.cc-bars.hero { row-gap: .6rem; grid-template-columns: minmax(9rem, 22%) 1fr auto; }
.cc-bars.hero .cc-bar-label { font-size: 1rem; font-weight: 600; }
.cc-bars.hero .cc-bar-track { height: 1.75rem; }
.cc-bars.hero .cc-bar-val { font-family: 'Archivo', sans-serif; font-size: 1.15rem; }
.cc-section { margin-top: 1.1rem !important; }
@media (max-width: 640px) {
  .cc-bars, .cc-bars.hero { grid-template-columns: 1fr auto; row-gap: .2rem; }
  .cc-bar-label { grid-column: 1 / -1; margin-top: .35rem; }
}

/* ---- Overview: CSS donut with a counted legend ---- */
.cc-donut-wrap { display: flex; align-items: center; gap: 1.6rem; flex-wrap: wrap; margin: .5rem 0 1rem; }
.cc-donut { position: relative; width: 11.5rem; aspect-ratio: 1; border-radius: 50%; flex: none; }
.cc-donut-hole { position: absolute; inset: 21%; border-radius: 50%; background: var(--steel);
                 display: flex; flex-direction: column; align-items: center; justify-content: center; }
.cc-donut-hole b { font-family: 'Archivo', sans-serif; font-weight: 900; font-size: 1.9rem;
                   line-height: 1; letter-spacing: -0.03em; color: var(--ink); }
.cc-donut-hole span { font-size: .82rem; color: var(--muted); margin-top: .15rem; }
.cc-legend { list-style: none; margin: 0; padding: 0; display: grid;
             grid-template-columns: auto 1fr auto auto; column-gap: .6rem; row-gap: .45rem;
             align-items: center; flex: 1; min-width: 12rem; }
.cc-legend li { display: contents; }
.cc-legend i { width: .8rem; height: .8rem; border-radius: 2px; box-shadow: inset 0 0 0 1px rgba(28,43,54,.15); }
.cc-legend span { font-size: .92rem; color: var(--ink); }
.cc-legend b { font-variant-numeric: tabular-nums; text-align: right; }
.cc-legend small { color: var(--muted); font-size: .8rem; text-align: right; min-width: 2.4rem; }
/* ---- Landing (top of Overview) ---- */
#todays-inbox { scroll-margin-top: 4.5rem; }
[data-testid="stMain"], html { scroll-behavior: smooth; }
@media (prefers-reduced-motion: reduce) { [data-testid="stMain"], html { scroll-behavior: auto; } }
.cc-landing { display: grid; grid-template-columns: minmax(0, 1.15fr) minmax(0, 1fr);
              gap: 2.5rem; align-items: center; min-height: 82vh; padding: 1rem 0 2.5rem;
              border-bottom: 2px solid var(--ink); margin-bottom: 2.2rem; }
.cc-landing .cc-kicker em { font-family: 'Instrument Serif', Georgia, serif; font-style: italic;
                            text-transform: none; font-weight: 400; letter-spacing: 0; font-size: .95rem; }
.cc-landing-title { font-family: 'Archivo', system-ui, sans-serif; font-weight: 900;
                    font-size: clamp(2.6rem, 6.4vw, 5.4rem); line-height: .98; letter-spacing: -0.035em;
                    color: var(--ink); margin: .2rem 0 1.1rem; padding: 0; }
.cc-landing-title em { font-family: 'Instrument Serif', Georgia, serif; font-style: italic;
                       font-weight: 400; letter-spacing: -0.01em; color: var(--mismatch); }
.cc-landing-title .hl { background: linear-gradient(transparent 62%, #F9C74F 62%); padding: 0 .06em; }
.cc-landing-sub { font-size: 1.12rem; line-height: 1.55; color: var(--muted); max-width: 46ch;
                  margin: 0 0 1.6rem; }
.cc-landing-steps { list-style: none; margin: 0 0 1.8rem; padding: 0; display: grid;
                    grid-template-columns: repeat(3, minmax(0, 1fr)); gap: .8rem; }
.cc-landing-steps li { border-top: 2px solid var(--ink); padding-top: .5rem; }
.cc-landing-steps b { display: block; font-family: 'Archivo', sans-serif; font-weight: 900;
                      font-size: 2rem; line-height: 1; letter-spacing: -0.03em; }
.cc-landing-steps span { display: block; margin-top: .3rem; font-size: .9rem; line-height: 1.3;
                         color: var(--muted); }
.cc-landing-steps em { font-family: 'Instrument Serif', Georgia, serif; font-size: 1.05rem; color: var(--ink); }
.cc-scrollcue { display: inline-flex; gap: .5rem; align-items: center; padding: .7rem 1.1rem;
                background: var(--ink); color: #fff !important; text-decoration: none !important;
                font-weight: 600; border-radius: 3px; box-shadow: 4px 4px 0 var(--mismatch); }
.cc-scrollcue:hover { transform: translate(-1px, -1px); box-shadow: 5px 5px 0 var(--mismatch); }
.cc-scrollcue span { display: inline-block; animation: cc-bob 1.6s ease-in-out infinite; }
@keyframes cc-bob { 50% { transform: translateY(3px); } }
@media (prefers-reduced-motion: reduce) { .cc-scrollcue span { animation: none; } }

.cc-landing-art { position: relative; min-height: 24rem; }
.cc-paper { position: absolute; width: 78%; background: #fff; border: 2px solid var(--ink);
            box-shadow: 6px 6px 0 var(--ink); padding: 1rem 1.1rem .6rem; font-size: .9rem;
            background-image: repeating-linear-gradient(transparent 0 2.05rem, rgba(28,43,54,.05) 2.05rem 2.1rem); }
.cc-paper.si { top: 0; left: 0; transform: rotate(-3deg); }
.cc-paper.bl { top: 8.2rem; right: 0; transform: rotate(2.5deg); }
.cc-paper .t { font-family: 'Archivo', sans-serif; font-weight: 800; font-size: .72rem;
               letter-spacing: .14em; text-transform: uppercase; color: var(--muted); margin: 0 0 .5rem; }
.cc-paper p { margin: 0; padding: .32rem 0; display: flex; justify-content: space-between; gap: 1rem;
              font-weight: 600; font-size: .84rem; }
.cc-paper p span { color: var(--muted); font-weight: 400; }
.cc-paper p.bad { background: #FDF1EA; box-shadow: inset 4px 0 0 var(--mismatch);
                  padding-left: .5rem; color: var(--mismatch); }
.cc-stamp { position: absolute; right: -2%; top: 6.4rem; transform: rotate(9deg); z-index: 2;
            border: 3px solid var(--mismatch); color: var(--mismatch); padding: .35rem .7rem;
            font-family: 'Archivo', sans-serif; font-weight: 900; text-transform: uppercase;
            letter-spacing: .08em; line-height: 1.05; text-align: center; background: rgba(255,255,255,.85); }
.cc-stamp small { font-size: .95rem; letter-spacing: 0; }
@media (max-width: 900px) {
  .cc-landing { grid-template-columns: 1fr; min-height: 0; gap: 1.8rem; }
  .cc-landing-art { min-height: 19rem; max-width: 30rem; }
}
@media (max-width: 640px) {
  .cc-landing-steps { grid-template-columns: 1fr; gap: .6rem; }
  .cc-landing-steps li { display: flex; gap: .8rem; align-items: baseline; }
  .cc-landing-steps span { margin: 0; }
  .cc-landing-art { min-height: 20rem; }
  .cc-paper { width: 86%; font-size: .8rem; }
  .cc-paper.bl { top: 7.4rem; }
  .cc-stamp { top: 5.6rem; right: 0; font-size: .85rem; }
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

# --- Landing (top of the Overview page) --------------------------------------

def landing(results):
    """First screen: what CargoCheck does, in five seconds. Scroll down for the dashboard."""
    total = len(results)
    checks = sum(1 for r in results.values() if r["data"].get("category") == "BL_COMPARISON")
    people = sum(1 for r in results.values()
                 if r["data"].get("category") == "BL_COMPARISON" and r["status"] in OPEN_STATUSES)
    st.markdown(f"""
<section class="cc-landing">
  <div class="cc-landing-text">
    <p class="cc-kicker">For shipping documentation teams</p>
    <h1 class="cc-landing-title">Catch the <em>wrong</em> BL before it <span class="hl">sails</span></h1>
    <p class="cc-landing-sub">CargoCheck reads the shipping inbox, compares every draft Bill of Lading
      with its Shipping Instruction, and flags what doesn't match. People only look at what
      actually needs them.</p>
    <ol class="cc-landing-steps">
      <li><b>{total}</b><span>emails <em>sorted</em> into five types</span></li>
      <li><b>{checks}</b><span>draft BLs <em>compared</em>, field by field</span></li>
      <li><b>{people}</b><span>unclear cases sent to <em>a person</em></span></li>
    </ol>
    <a class="cc-scrollcue" href="#todays-inbox">See today's overview <span aria-hidden="true">↓</span></a>
  </div>
  <div class="cc-landing-art" aria-hidden="true">
    <div class="cc-paper si"><p class="t">Shipping Instruction</p>
      <p class="bad"><span>Containers</span>3 x 40'HC</p>
      <p><span>Consignee</span>PACIFIC OFFICE SDN BHD</p>
      <p><span>Port of discharge</span>MERSIN (TRMER)</p>
      <p><span>Gross weight</span>22,000 KG</p></div>
    <div class="cc-paper bl"><p class="t">Draft Bill of Lading</p>
      <p class="bad"><span>Containers</span>4 x 40'HC</p>
      <p><span>Consignee</span>PACIFIC OFFICE SDN BHD</p>
      <p><span>Port of discharge</span>MERSIN (TRMER)</p>
      <p><span>Gross weight</span>22,000 KG</p></div>
    <div class="cc-stamp">Mismatch<br><small>3 ≠ 4</small></div>
  </div>
</section>
<div id="todays-inbox"></div>""", unsafe_allow_html=True)


# --- Page: Overview ----------------------------------------------------------

def bar_list(rows, hero=False):
    """Labelled horizontal bars in plain HTML: label, bar, value written at the end.
    rows: dicts with label, value, color, and optional note (shown after the value).
    No legend to decode, long labels wrap, and it stacks on phones."""
    top = max((r["value"] for r in rows), default=0) or 1
    items = "".join(
        f'<li class="cc-bar-row"><span class="cc-bar-label">{html.escape(str(r["label"]))}</span>'
        f'<span class="cc-bar-track"><span class="cc-bar-fill" '
        f'style="width:{max(r["value"] / top * 100, 1.5):.1f}%;background:{r["color"]}"></span></span>'
        f'<span class="cc-bar-val">{r["value"]}'
        f'{" <small>" + html.escape(r["note"]) + "</small>" if r.get("note") else ""}</span></li>'
        for r in rows)
    st.markdown(f'<ul class="cc-bars{" hero" if hero else ""}">{items}</ul>', unsafe_allow_html=True)


def donut_html(rows, center_value, center_label):
    """A CSS donut (no chart library): colours are clearly different, the total sits in
    the middle, and the legend beside it shows each count and share."""
    total = sum(r["value"] for r in rows) or 1
    stops, at = [], 0.0
    for r in rows:
        share = r["value"] / total * 100
        stops.append(f'{r["color"]} {at:.2f}% {at + share:.2f}%')
        at += share
    legend = "".join(
        f'<li><i style="background:{r["color"]}"></i><span>{html.escape(r["label"])}</span>'
        f'<b>{r["value"]}</b><small>{r["value"] / total:.0%}</small></li>' for r in rows)
    aria = ", ".join(f'{r["label"]} {r["value"]}' for r in rows)
    st.markdown(f"""
<div class="cc-donut-wrap">
  <div class="cc-donut" role="img" aria-label="{html.escape(aria)}"
       style="background:conic-gradient({', '.join(stops)})">
    <div class="cc-donut-hole"><b>{center_value}</b><span>{center_label}</span></div>
  </div>
  <ul class="cc-legend">{legend}</ul>
</div>""", unsafe_allow_html=True)


def insight(text):
    st.markdown(f'<p class="cc-insight">{text}</p>', unsafe_allow_html=True)


def section(title):
    st.markdown(f'<p class="cc-section">{title}</p>', unsafe_allow_html=True)


def page_overview(results):
    landing(results)
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

    # time-saved estimate: the inputs sit under the cards, their values are read here first
    per_check = st.session_state.get("per_check", MINUTES_PER_CHECK)
    per_sort = st.session_state.get("per_sort", MINUTES_PER_SORT)
    hours = (len(compared) * per_check + len(processed) * per_sort) / 60

    with st.expander(f"Time saved: ~{hours:.0f}h of manual checking · how it's estimated"):
        st.caption(f"{len(compared)} SI/BL comparisons plus sorting {len(processed)} emails, "
                   "at the times below. Change them to match your team.")
        c1, c2 = st.columns(2)
        c1.number_input("Minutes to compare one SI and BL by hand", 1.0, 60.0,
                        MINUTES_PER_CHECK, 0.5, key="per_check")
        c2.number_input("Minutes to read and sort one email", 0.1, 10.0,
                        MINUTES_PER_SORT, 0.1, key="per_sort")

    # 1. hero: which fields go wrong most often
    section("What goes wrong most often")
    field_counts = pd.Series([f for r in mismatches for f in r["entry"]["defect_fields"]],
                             dtype="object").value_counts()
    if len(field_counts):
        bar_list([{"label": FIELD_LABELS[f], "value": int(n), "color": PALETTE["mismatch"]}
                  for f, n in field_counts.items()], hero=True)
        top_field, top_n = FIELD_LABELS[field_counts.index[0]], int(field_counts.iloc[0])
        insight(f"<b>{top_field}</b> is the most common error: wrong on {top_n} of "
                f"{len(mismatches)} draft BLs with mismatches.")
    else:
        st.write("No mismatches found yet.")

    left, right = st.columns([1.15, 1], gap="large")

    # 2. which shippers' BLs have the most mismatches, 3. how documents were read
    with left:
        section("Shippers with the most mismatched BLs")
        names, per_shipper = {}, {}
        for r in compared:
            raw = next((row["si"] for row in r["report"].get("fields", [])
                        if row["field"] == "shipper" and row["si"]), None)
            if not raw:
                continue
            key = normalize_party(raw)
            names.setdefault(key, str(raw).split(" ON BEHALF OF")[0].strip())
            total, bad = per_shipper.get(key, (0, 0))
            per_shipper[key] = (total + 1, bad + (r["status"] == "MISMATCH"))
        ranked = sorted(((names[k], bad, total) for k, (total, bad) in per_shipper.items() if bad),
                        key=lambda x: (-x[1], x[0]))[:6]
        if ranked:
            bar_list([{"label": name, "value": bad, "color": PALETTE["ink"],
                       "note": f"of {total} · {bad / total:.0%}"} for name, bad, total in ranked])
            name, bad, total = ranked[0]
            insight(f"<b>{html.escape(name)}</b> has the most mismatched BLs "
                    f"({bad} of {total} checks).")
        else:
            st.write("No mismatches found yet.")

        section("How the documents were read")
        if len(methods):
            colors = {"Rules (no AI)": PALETTE["match"], "AI (LLM)": PALETTE["ink"],
                      "AI vision (scans)": PALETTE["review"]}
            bar_list([{"label": m, "value": int(methods[m]), "color": colors[m]}
                      for m in colors if m in methods])
            insight(f"<b>{rules_share:.0%}</b> read by free, instant rules. AI only steps in "
                    "where the rules can't, and scans always go to a person to confirm.")
        else:
            st.write("No documents read yet.")

    # 4. inbox breakdown
    with right:
        section("Inbox at a glance")
        cats = pd.Series([r["data"].get("category") for r in processed],
                         dtype="object").value_counts()
        if len(cats):
            colors = {"BL_COMPARISON": PALETTE["ink"], "SI_REQUEST": PALETTE["match"],
                      "INVOICE_QUERY": "#F2B33D", "GENERAL": "#8FA3B3", "SPAM": "#D9DFE3"}
            order = [c for c in CATEGORIES if c in cats]
            donut_html([{"label": CATEGORY_LABELS[c], "value": int(cats[c]), "color": colors[c]}
                        for c in order], len(processed), "emails")
            insight(f"<b>{len(checks)}</b> of {len(processed)} emails ask for a BL check; "
                    "the rest only needed sorting.")

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