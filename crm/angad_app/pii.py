"""
PII handling — no mobile numbers leave Mongo.

Every phone number is replaced with a deterministic pseudonym before it is
written to the local cache, the report, or any CSV export:

    +919618988266  ->  LEAD-7f3a91c2

Deterministic means the same number always maps to the same ref, so joins
across calls / leads / scheduledcalls still work and per-lead history is intact
— but the number itself is never persisted anywhere outside Mongo.

The salt is generated once into data/.salt and is gitignored. Without that file
the mapping cannot be reversed. Delete it and every ref changes.

PII policy 2026-08-06 (user instruction): pseudonyms are permanently ONE-WAY.
The ref->phone map and its builder were deleted; write_map raises. Personal
names are never cached. To act on a specific lead, look the pseudonym's
activity up here, then find the person in Zoho/Compass directly.
"""
import hashlib
import os
import re
from pathlib import Path

DATA = Path(__file__).parent / "data"
SALT_FILE = DATA / ".salt"

_salt = None


def _get_salt():
    global _salt
    if _salt is None:
        DATA.mkdir(exist_ok=True)
        if SALT_FILE.exists():
            _salt = SALT_FILE.read_bytes()
        else:
            _salt = os.urandom(32)
            SALT_FILE.write_bytes(_salt)
    return _salt


def digits(v):
    """Normalize to the last 10 digits.

    Phone formats differ across the four databases (+919618988266, 919618988266,
    9618988266), and the cross-database join is BY PHONE — so every store must
    normalize identically or the same person becomes three different refs.
    Last-10 is the rule the data dictionary specifies."""
    d = re.sub(r"\D", "", str(v or ""))
    return d[-10:] if len(d) >= 10 else d


def ref(phone):
    """Deterministic pseudonym for a phone number. None stays None."""
    d = digits(phone)
    if len(d) < 10:
        return None
    h = hashlib.sha256(_get_salt() + d.encode()).hexdigest()[:8]
    return f"LEAD-{h}"


_CAND = re.compile(r"(\+?\d[\d\-\s().]{8,}\d)")
_DATEISH = re.compile(r"^(19|20)\d{2}[-/.]")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]{2,}")


def redact(text):
    """Strip anything phone- or email-shaped out of free text.

    A phone candidate is only redacted when it carries >= 10 digits AND does
    not look like a date. The first version redacted any long digit run, which
    silently destroyed ISO dates ("2026-07-23" -> "[number]") in string-typed
    columns — real phone numbers always have 10+ digits, dates have 8.
    Emails are masked too: the ops sheets have them typed into name fields.
    """
    if not text:
        return text

    def _sub(m):
        cand = m.group(1)
        ndigits = len(re.sub(r"\D", "", cand))
        # Phone numbers carry 10-14 digits (10 bare, 12 with +91, up to 14
        # with punctuation variants). Longer runs are SYSTEM IDS - Zoho lead
        # ids are 19 digits, external_schedule_ids embed them - and masking
        # those corrupted 11k+ cross-store joins (found by the pipeline
        # trace). Only the phone band is redacted.
        if ndigits < 10 or ndigits > 14 or _DATEISH.match(cand.strip()):
            return cand
        return "[number]"

    return _EMAIL.sub("[email]", _CAND.sub(_sub, str(text)))


# -------------------------------------------------------------- name leaks
# Imprint 2 says personal names are NEVER cached. `audit_pii.py` only looks for
# phones, emails and credentials, so it cannot see a name and reported CLEAN on
# a cache that contained one. Found 2026-08-08 in
# Zoho_Database.call_transcripts: the agent opens with "Good evening, <Name>."
#
# General name detection is not solvable with a regex and we will not pretend
# otherwise. What IS reliably detectable is the VOCATIVE — a capitalised token
# directly addressed after a greeting or before a comma at the start of a turn.
# That is where the customer's name actually appears in these transcripts.
# Everything else in the text is dialogue about property, not identity.
# NOTE: the greeting alternation is case-insensitive via an inline group, but
# the NAME group must stay case-SENSITIVE. A module-level re.I makes [A-Z][a-z]
# match anything, which redacted "Hi, this is Anandita" down to "Hi, [name] is"
# — destroying the agent-self-introduction signal we use to date the persona
# switch. Scope the flag, never apply it to the whole pattern.
_VOCATIVE = re.compile(
    r"(?i:\b(hello|hi|hey|good\s+(?:morning|afternoon|evening)|namaste|namaskar|"
    r"am\s+i\s+speaking\s+(?:to|with))\b)([,!]?\s+)"
    r"(?!Sir|Madam|Ma'?am|There|Ji\b|This|That|It|I\b|We\b|My\b|"
    r"Mr\b|Mrs\b|Ms\b|Miss\b|Shri\b|Smt\b)"
    r"([A-Z][a-z]{1,20})")
_HONORIFIC = re.compile(r"\b(Mr|Mrs|Ms|Miss|Shri|Smt)\.?\s+([A-Z][a-z]{1,20})")


def strip_names(text):
    """Remove addressed personal names from transcript / summary free text.

    Conservative by design: it redacts the vocative slot and honorific+name,
    and leaves ordinary prose alone. Run it IN ADDITION to redact(), never
    instead of it."""
    if not text:
        return text
    # honorific FIRST: "Good morning, Mr. Sharma" must not have the vocative
    # consume "Mr" and leave "Sharma" standing in the clear.
    t = _HONORIFIC.sub(lambda m: f"{m.group(1)}. [name]", str(text))
    return _VOCATIVE.sub(lambda m: f"{m.group(1)}{m.group(2)}[name]", t)


def clean_text(text):
    """The full free-text gate: phones, emails, then addressed names."""
    return strip_names(redact(text))


def write_map(path):
    raise RuntimeError("PII policy 2026-08-06: the ref->phone map is banned. "
                       "Pseudonyms are one-way by design.")
