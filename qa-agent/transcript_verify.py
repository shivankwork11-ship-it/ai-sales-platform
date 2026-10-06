"""
transcript_verify.py — Post-processing verification for QA issue citations.

After the LLM returns its issues, we verify every quoted phrase actually
exists in the transcript. If it does, we correct the turn number. If it
doesn't, we drop the issue entirely (hallucinated).

This eliminates two classes of false positives:
  1. LLM cites the right phrase but wrong turn number → corrected
  2. LLM invents a phrase that was never spoken → dropped
"""

import re


def extract_first_quote(detail: str) -> str:
    """
    Pull the first single-quoted phrase from a detail string.
    e.g. "Anandita said 'one ninety-five' in [Turn 6]" → "one ninety-five"
    """
    match = re.search(r"'([^']+)'", detail)
    return match.group(1).strip() if match else ""


def find_quote_in_transcript(transcript_text: str, quote: str) -> int | None:
    """
    Search the formatted transcript (with [Turn N] prefixes) for the quoted phrase.
    Returns the turn index if found, None if not found anywhere.
    Case-insensitive, ignores punctuation differences.
    """
    if not quote:
        return None

    quote_clean = quote.lower().strip()

    for line in transcript_text.splitlines():
        match = re.match(r'\[Turn (\d+)\] Anandita: (.+)', line)
        if not match:
            continue
        turn_num  = int(match.group(1))
        turn_text = match.group(2).lower().strip()

        if quote_clean in turn_text:
            return turn_num

    return None


def fix_turn_number(detail: str, correct_turn: int) -> str:
    """Replace whatever [Turn N] is in the detail string with the correct one."""
    return re.sub(r'\[Turn \d+\]', f'[Turn {correct_turn}]', detail)


def verify_and_fix_issues(issues: list, transcript_text: str) -> list:
    """
    For each issue:
      - Extract the quoted phrase from detail
      - Search the actual transcript for it
      - If found → fix the turn number
      - If not found → drop the issue (hallucinated quote)

    Returns the cleaned issue list.
    """
    verified = []
    for issue in issues:
        detail = issue.get("detail", "")
        quote  = extract_first_quote(detail)

        if not quote:
            # No quote to verify — keep as-is
            verified.append(issue)
            continue

        correct_turn = find_quote_in_transcript(transcript_text, quote)

        if correct_turn is None:
            # Quote not found anywhere in Anandita's turns → hallucinated → drop
            print(f"[QA Verify] Dropping hallucinated issue — quote not found: '{quote}'")
            continue

        # Fix the turn number if it was wrong
        old_detail = detail
        issue["detail"] = fix_turn_number(detail, correct_turn)
        if issue["detail"] != old_detail:
            print(f"[QA Verify] Fixed turn number for: '{quote}'")

        verified.append(issue)

    return verified
