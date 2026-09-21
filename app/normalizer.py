"""
normalizer.py - cleans extracted SI/BL values so equivalent things compare equal.

Input:  raw strings from the extractor, e.g. "20,381 KG" or "1 x 40'HC"
Output: clean values, e.g. 20381.0 or 1
Returns None when a value is missing or can't be understood, so the comparer
can send the email to NEEDS_REVIEW (missing_value) instead of guessing.
"""

import re

# --- Step 1: shared helpers -------------------------------------------------

MISSING_WORDS = {"", "N/A", "NA", "NIL", "NONE", "TBA", "TBC", "-", "--"}


def is_missing(value):
    """True if the extractor gave us nothing usable."""
    if value is None:
        return True
    text = str(value).strip().upper()
    if text in MISSING_WORDS:
        return True
    # Blank form fields like "____" or "____MT": underscores with almost nothing else
    if "_" in text and len(re.sub(r"[^A-Z0-9]", "", text)) <= 3:
        return True
    return False


def clean_text(value):
    """Uppercase, turn punctuation into spaces, collapse repeated spaces."""
    text = str(value).upper()
    text = text.replace("&", " AND ")
    text = re.sub(r"[^A-Z0-9 ]", " ", text)   # punctuation -> space
    return re.sub(r"\s+", " ", text).strip()


# --- Step 2: company names (shipper, consignee, notify_party) ---------------

# Different ways of writing the same company suffix. Applied AFTER clean_text,
# so "SDN. BHD." has already become "SDN BHD".
SUFFIXES = [
    (r"\bSENDIRIAN BERHAD\b", "SDN BHD"),
    (r"\bPRIVATE LIMITED\b", "PTE LTD"),
    (r"\bPVT LTD\b", "PTE LTD"),
    (r"\bLIMITED\b", "LTD"),
    (r"\bCOMPANY\b", "CO"),
    (r"\bCORPORATION\b", "CORP"),
    (r"\bINCORPORATED\b", "INC"),
]


def normalize_party(value):
    if is_missing(value):
        return None
    text = clean_text(value)
    for pattern, replacement in SUFFIXES:
        text = re.sub(pattern, replacement, text)
    return text


# --- Step 3: ports (port_of_loading, port_of_discharge) ---------------------

# UN/LOCODE: 2-letter country + 3-character place, e.g. (MYPKG), (TRMER)
LOCODE = re.compile(r"\(([A-Z]{2}[A-Z0-9]{3})\)")


# Five-letter words that look like port codes but aren't
NOT_CODES = {"NORTH", "SOUTH", "WHARF", "INNER", "OUTER"}
# Words that don't identify a port on their own
GENERIC_PORT_WORDS = {"PORT", "OF", "THE", "CITY", "HARBOUR", "HARBOR", "TERMINAL"}


def city_words(text):
    """The words that name the city, without the country.

    'PORT KLANG (WESTPORT), MALAYSIA' -> {'KLANG', 'WESTPORT'}
    'TUTICORIN, INDIA'                -> {'TUTICORIN'}
    'NHAVA SHEVA INDIA'               -> {'NHAVA', 'SHEVA', 'INDIA'}  (no comma: keep all)
    """
    parts = [p for p in text.split(",") if p.strip()]
    if len(parts) >= 2:
        parts = parts[:-1]                   # last comma part is the country
    words = set(clean_text(" ".join(parts)).split())
    return frozenset(words - GENERIC_PORT_WORDS)


def normalize_port(value):
    """Return (code, city_words). The comparer needs BOTH to agree.

    The data contains traps where the city changes but the code in brackets
    stays the same, e.g. 'MOMBASA, KENYA (KEMBA)' vs 'TUTICORIN, INDIA (KEMBA)',
    so the code alone can't be trusted.
    """
    if is_missing(value):
        return None
    text = str(value).upper()
    codes = [c for c in LOCODE.findall(text) if c not in NOT_CODES]
    for c in codes:
        text = text.replace(f"({c})", " ")   # drop the code, keep other brackets like (WESTPORT)
    return (codes[-1] if codes else None, city_words(text))


def ports_match(si_port, bl_port):
    """Codes must agree (when both have one) AND the city names must overlap."""
    si_code, si_city = si_port
    bl_code, bl_city = bl_port
    if si_code and bl_code and si_code != bl_code:
        return False
    if si_city and bl_city:
        return bool(si_city & bl_city)
    return bool(si_code and bl_code)         # no usable name on one side: fall back to codes


# --- Step 4: container count ------------------------------------------------

def normalize_container_count(value):
    """'1 x 40'HC' -> 1, '2 x 20GP + 1 x 40HC' -> 3, '3 CONTAINERS' -> 3."""
    if is_missing(value):
        return None
    if isinstance(value, int):
        return value
    text = str(value).upper()

    # "2 x 20GP", "(1) X 40'HC" - add up every "N x" group
    groups = re.findall(r"(\d+)\)?\s*[X×]", text)
    if groups:
        return sum(int(n) for n in groups)

    # "3 CONTAINERS", "3 CNTRS"
    match = re.search(r"(\d+)\s*(CONTAINERS?|CNTRS?|CTNRS?|UNITS?)\b", text)
    if match:
        return int(match.group(1))

    # a bare number like "3"
    if re.fullmatch(r"\s*\d+\s*", text):
        return int(text)

    return None   # can't tell safely -> let a human decide


# --- Step 5: gross weight in kg ---------------------------------------------

def parse_number(num):
    """Turn '20,381', '20.381', '20381.000' or '20.381,50' into a float."""
    num = num.replace(" ", "")
    if "," in num and "." in num:
        # whichever separator comes last is the decimal point
        if num.rfind(",") > num.rfind("."):
            num = num.replace(".", "").replace(",", ".")
        else:
            num = num.replace(",", "")
    elif "," in num:
        # "20,381" = thousands; "20,5" = decimal
        if re.fullmatch(r"\d{1,3}(,\d{3})+", num):
            num = num.replace(",", "")
        else:
            num = num.replace(",", ".")
    elif num.count(".") > 1:
        num = num.replace(".", "")        # "1.020.381" = thousands
    return float(num)


def normalize_weight(value):
    """Return weight in kg as a float, converting MT/tonnes and lbs."""
    if is_missing(value):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).upper()

    match = re.search(r"\d[\d.,\s]*", text)
    if not match:
        return None
    try:
        number = parse_number(match.group().strip())
    except ValueError:
        return None

    unit_text = text[match.end():]
    if re.search(r"\b(MT|MTS|TONNES?|TONS?|T)\b", unit_text):
        number *= 1000
    elif re.search(r"\b(LBS?|POUNDS?)\b", unit_text):
        number *= 0.45359237
    return round(number, 2)


# --- Step 6: one entry point for the comparer -------------------------------

NORMALIZERS = {
    "shipper": normalize_party,
    "consignee": normalize_party,
    "notify_party": normalize_party,
    "port_of_loading": normalize_port,
    "port_of_discharge": normalize_port,
    "container_count": normalize_container_count,
    "gross_weight_kg": normalize_weight,
}


def normalize_fields(fields):
    """Normalize a dict of the 7 fields from one document (SI or BL)."""
    return {name: NORMALIZERS[name](fields.get(name)) for name in NORMALIZERS}


# --- Step 7: quick self-test using email_009 --------------------------------

if __name__ == "__main__":
    si = {
        "shipper": "ASIA PACIFIC PAPERBOARD TRADING PTE LTD",
        "consignee": "PACIFIC OFFICE (M) SDN BHD",
        "notify_party": "PACIFIC OFFICE (M) SDN BHD",
        "port_of_loading": "PORT KLANG (WESTPORT), MALAYSIA (MYPKG)",
        "port_of_discharge": "MERSIN, TURKEY (TRMER)",
        "container_count": "1 x 40'HC",
        "gross_weight_kg": "20,381 KG",
    }
    print("email_009 SI ->", normalize_fields(si))

    tests = [
        (normalize_weight, "22 MT", 22000.0),
        (normalize_weight, "20381.000 KGS", 20381.0),
        (normalize_weight, "20.381,50 KG", 20381.5),
        (normalize_container_count, "2 x 20GP + 1 x 40HC", 3),
        (normalize_container_count, "ONE (1) X 40'HC", 1),
        (normalize_party, "Pacific Office (M) Sdn. Bhd.", "PACIFIC OFFICE M SDN BHD"),
        (normalize_port, "TUTICORIN, INDIA (KEMBA)", ("KEMBA", frozenset({"TUTICORIN"}))),
        (normalize_port, "NANTONG, CHINA", (None, frozenset({"NANTONG"}))),
        (normalize_port, "____MT", None),
        (normalize_weight, "TBA", None),
    ]
    for func, raw, expected in tests:
        got = func(raw)
        mark = "PASS" if got == expected else "FAIL"
        print(f"{mark}  {func.__name__}({raw!r}) = {got!r}  (expected {expected!r})")