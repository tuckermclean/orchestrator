"""session_limit.py — Session/usage-limit detection for Claude Code CLI output.

Claude Code emits a distinctive message when the operator's Anthropic account
has hit its session or usage limit:

    You've hit your session limit · resets <time> <tz>
    You've hit your usage limit · resets <time> <tz>

The Claude Code harness exits with a non-zero code in this scenario.  Without
special handling the run is recorded as ``failed`` and the work is discarded.

This module provides two pure functions:

  ``is_session_limit(text)``
      Returns True when *text* matches any of Claude's known session/usage-limit
      output patterns, or when the raw output indicates a 429/quota-exhausted
      upstream error that Claude Code surfaces.

  ``parse_reset_time(text)``
      Attempts to extract the reset timestamp from the message.  Returns an
      ISO-8601 UTC string when a parseable time is found, else None.

Design decisions
----------------
- Detection is text-based, not exit-code-based: Claude Code does not reserve a
  distinct exit code for quota exhaustion; the exit code is always non-zero on
  failure.  Text detection against the collected event stream (agent_result,
  agent_message) is the reliable path.
- These are pure synchronous functions (AGENTS.md §5 / SPEC §8 purity contract).
  No I/O, no async.
- Detection operates on the FULL collected text from the run, not line-by-line,
  so partial matches across buffered lines cannot be missed.
- ``agents/**`` and ``.agents/**`` are PROTECTED — no changes there (AGENTS.md §9).
  Detection is entirely harness-side, off Claude's native output.

Security (I3): the text passed to these functions is untrusted agent output.
Both functions only match regex patterns against the text; they do NOT evaluate
the output as code or HTML.  parse_reset_time further validates the extracted
time string through Python's standard datetime parser.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta, timezone
from typing import Final

# ---------------------------------------------------------------------------
# Detection patterns
# ---------------------------------------------------------------------------

# Claude Code's own session/usage-limit message (case-insensitive to survive
# future capitalization changes and internationalisation).
_SESSION_LIMIT_RE: re.Pattern[str] = re.compile(
    r"you(?:'ve| have) hit your (?:session|usage) limit",
    re.IGNORECASE,
)

# HTTP-level quota exhaustion patterns surfaced by the Claude Code CLI
# (e.g. "429 Too Many Requests", "quota exhausted", "rate limit exceeded").
_HTTP_QUOTA_RE: re.Pattern[str] = re.compile(
    r"(?:429\s+[Tt]oo\s+[Mm]any|quota\s+exhausted|rate\s+limit\s+(?:exceeded|hit))",
    re.IGNORECASE,
)

# Combined pattern for both classes.
_ANY_LIMIT_RE: re.Pattern[str] = re.compile(
    r"(?:"
    r"you(?:'ve| have) hit your (?:session|usage) limit"
    r"|429\s+[Tt]oo\s+[Mm]any"
    r"|quota\s+exhausted"
    r"|rate\s+limit\s+(?:exceeded|hit)"
    r")",
    re.IGNORECASE,
)

# Pattern to extract the reset time from Claude's "resets <time> <tz>" suffix.
# Examples observed in the wild:
#   "resets 4:30 PM"
#   "resets 4:30 PM PDT"
#   "resets 16:30 UTC"
#   "resets 4:30 PM Pacific Time"
#   "resets in 5 minutes"
#
# We capture the token(s) after "resets" up to end-of-line or a sentence boundary.
# The extracted string is then passed to _parse_time_token for structured parsing.
_RESET_TIME_RE: re.Pattern[str] = re.compile(
    r"resets?\s+(.+?)(?:[·.!\n]|$)",
    re.IGNORECASE,
)

# Known timezone abbreviation → UTC offset mapping (hours).
# Limited to the most common US/international abbreviations seen in Claude output.
_TZ_OFFSETS: Final[dict[str, int]] = {
    "UTC": 0,
    "GMT": 0,
    "EST": -5,
    "EDT": -4,
    "CST": -6,
    "CDT": -5,
    "MST": -7,
    "MDT": -6,
    "PST": -8,
    "PDT": -7,
    "CEST": 2,
    "CET": 1,
    "BST": 1,
    "IST": 5,  # India Standard Time
    "JST": 9,
}

# Pattern for "HH:MM" or "H:MM" with optional "AM"/"PM" and optional tz abbreviation.
_TIME_HMS_RE: re.Pattern[str] = re.compile(
    r"(\d{1,2}):(\d{2})(?::(\d{2}))?\s*(AM|PM)?\s*([A-Z]{2,5})?",
    re.IGNORECASE,
)

# Pattern for "in N minutes" / "in N hours" relative resets.
_RELATIVE_RE: re.Pattern[str] = re.compile(
    r"in\s+(\d+)\s+(minute|hour|second)s?",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Public API — pure synchronous functions
# ---------------------------------------------------------------------------


def is_session_limit(text: str) -> bool:
    """Return True when *text* contains a session/usage-limit signature.

    Checks for:
      - Claude's own "You've hit your session/usage limit" message.
      - HTTP-level 429 / quota-exhausted / rate-limit-exceeded signals.

    Pure function: no I/O, no side effects (AGENTS.md §5).
    """
    return bool(_ANY_LIMIT_RE.search(text))


def parse_reset_time(text: str) -> str | None:
    """Extract the reset timestamp from Claude's session-limit message.

    Returns an ISO-8601 UTC string (e.g. ``"2026-06-24T21:30:00+00:00"``) when
    a parseable time is found, or ``None`` when parsing fails (the caller falls
    back to the fixed ``HARNESS_COOLDOWN_S`` in that case).

    Handles:
      - Absolute clock times with optional AM/PM and timezone abbreviation
        (e.g. "resets 4:30 PM PDT").
      - Relative durations (e.g. "resets in 5 minutes").
      - Falls back gracefully when neither pattern matches.

    Pure function: no I/O, no side effects.  ``datetime.now(UTC)`` is called
    once inside to anchor relative times — callers that need deterministic tests
    should pass pre-formatted timestamps via a fake ``is_session_limit`` path.
    """
    reset_match = _RESET_TIME_RE.search(text)
    if reset_match is None:
        return None

    time_token = reset_match.group(1).strip()
    return _parse_time_token(time_token)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _parse_time_token(token: str) -> str | None:
    """Attempt to parse a time token extracted from a reset-time message.

    Tries relative patterns first (fast, unambiguous), then absolute HH:MM.
    Returns an ISO-8601 UTC string or None.
    """
    # Relative: "in N minutes" / "in N hours" / "in N seconds"
    rel_match = _RELATIVE_RE.search(token)
    if rel_match is not None:
        qty = int(rel_match.group(1))
        unit = rel_match.group(2).lower()
        now_utc = datetime.now(UTC)
        if unit.startswith("second"):
            delta = timedelta(seconds=qty)
        elif unit.startswith("minute"):
            delta = timedelta(minutes=qty)
        else:  # hour
            delta = timedelta(hours=qty)
        return (now_utc + delta).isoformat()

    # Absolute: "H:MM AM/PM [TZ]"
    abs_match = _TIME_HMS_RE.search(token)
    if abs_match is None:
        return None

    hour = int(abs_match.group(1))
    minute = int(abs_match.group(2))
    second = int(abs_match.group(3)) if abs_match.group(3) else 0
    ampm = (abs_match.group(4) or "").upper()
    tz_abbr = (abs_match.group(5) or "").upper()

    # Apply AM/PM adjustment.
    if ampm == "PM" and hour != 12:
        hour += 12
    elif ampm == "AM" and hour == 12:
        hour = 0

    # Validate ranges.
    if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59):
        return None

    # Resolve timezone offset.
    tz_offset_h = _TZ_OFFSETS.get(tz_abbr, None)
    if tz_offset_h is not None:
        tz = timezone(timedelta(hours=tz_offset_h))
    else:
        # Unknown or absent tz abbreviation — treat as UTC.
        tz = UTC

    # Build candidate datetime using today's date (in the resolved tz).
    now_local = datetime.now(tz)
    candidate = now_local.replace(hour=hour, minute=minute, second=second, microsecond=0)

    # If the time has already passed today, assume it means tomorrow.
    if candidate <= now_local:
        candidate += timedelta(days=1)

    # Convert to UTC and return as ISO-8601.
    return candidate.astimezone(UTC).isoformat()
