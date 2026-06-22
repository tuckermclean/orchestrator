"""Unit tests for decide_round — SPEC §8.3 / TESTING.md §2."""

from __future__ import annotations

import pytest

from src.decisions.decide_round import decide_round

# ---------------------------------------------------------------------------
# Row 1 — approve: blockers == 0 and ci_green (any round)
# ---------------------------------------------------------------------------


def test_decide_round_approve_r1() -> None:
    assert decide_round(1, 0, True, [], []) == "approve"


def test_decide_round_approve_r2() -> None:
    assert decide_round(2, 0, True, [], []) == "approve"


def test_decide_round_approve_r3() -> None:
    assert decide_round(3, 0, True, [], []) == "approve"


def test_decide_round_zero_blockers_ci_red_not_approve_r1() -> None:
    """CI-green guard: 0 blockers but CI red → R1 falls through to fix, not approve."""
    assert decide_round(1, 0, False, [], []) == "fix"


def test_decide_round_unknown_never_approves_even_ci_green() -> None:
    """'unknown' blockers never produce approve (row 1 needs integer 0)."""
    assert decide_round(3, "unknown", True, [], []) == "escalate:no-verdict"


# ---------------------------------------------------------------------------
# Row 2 — R1 always fix (when not approve)
# ---------------------------------------------------------------------------


def test_decide_round_fix_r1_with_blockers() -> None:
    assert decide_round(1, 2, False, [], ["a:b"]) == "fix"


def test_decide_round_fix_r1_unknown() -> None:
    """R1 unknown blockers fall through to fix (not approve)."""
    assert decide_round(1, "unknown", True, [], []) == "fix"


# ---------------------------------------------------------------------------
# Row 3 — no-progress: same non-empty sigs two consecutive rounds
# ---------------------------------------------------------------------------


def test_decide_round_no_progress_r2() -> None:
    assert decide_round(2, 1, False, ["a:x"], ["a:x"]) == "escalate:no-progress"


def test_decide_round_no_progress_r3() -> None:
    assert decide_round(3, 1, False, ["a:x"], ["a:x"]) == "escalate:no-progress"


def test_decide_round_no_progress_sorts_before_compare() -> None:
    """Lexicographic sort: out-of-order identical sets still match."""
    assert (
        decide_round(2, 2, False, ["b:y", "a:x"], ["a:x", "b:y"])
        == "escalate:no-progress"
    )


def test_decide_round_empty_sigs_not_no_progress() -> None:
    """prev==curr==[] is NOT no-progress (row 3 needs non-empty curr_sigs)."""
    assert decide_round(2, 1, False, [], []) == "fix"


def test_decide_round_sentinel_normalized_not_no_progress() -> None:
    """Both sides sentinel → normalized to [] → not no-progress."""
    sentinel = ["verdict-file-not-written"]
    assert decide_round(2, 1, False, sentinel, sentinel) == "fix"


def test_decide_round_no_progress_skipped_when_blockers_zero() -> None:
    """blockers==0 (CI red) with stable sigs: row 3 excludes 0 → R2 fix."""
    assert decide_round(2, 0, False, ["a:x"], ["a:x"]) == "fix"


# ---------------------------------------------------------------------------
# Row 4 — R2 fix (when not approve / not no-progress)
# ---------------------------------------------------------------------------


def test_decide_round_fix_r2_changed_sigs() -> None:
    assert decide_round(2, 1, False, ["a:x"], ["a:y"]) == "fix"


# ---------------------------------------------------------------------------
# Rows 5–7 — R3 terminal outcomes
# ---------------------------------------------------------------------------


def test_decide_round_r3_no_verdict() -> None:
    assert decide_round(3, "unknown", False, [], []) == "escalate:no-verdict"


def test_decide_round_r3_ci_red() -> None:
    """R3, blockers cleared but CI not green → ci-red."""
    assert decide_round(3, 0, False, [], []) == "escalate:ci-red"


def test_decide_round_r3_cap_reached() -> None:
    assert decide_round(3, 2, False, ["a:x"], ["a:y"]) == "escalate:cap-reached"


def test_decide_round_r3_no_progress_fires_before_cap() -> None:
    """Row 3 fires before rows 5–7 in R3 when sigs are stable."""
    assert decide_round(3, 2, False, ["a:x"], ["a:x"]) == "escalate:no-progress"


# ---------------------------------------------------------------------------
# Runtime validation — Literal[1,2,3] / bool / int|"unknown" enforced (TESTING.md §2.4)
# ---------------------------------------------------------------------------


def test_decide_round_invalid_round_zero() -> None:
    """round == 0 is outside Literal[1,2,3] → TypeError (SPEC §8.3)."""
    with pytest.raises(TypeError):
        decide_round(0, 0, True, [], [])  # type: ignore[arg-type]


def test_decide_round_invalid_round_four() -> None:
    """round == 4 is outside Literal[1,2,3] → TypeError (must not fall through to R3)."""
    with pytest.raises(TypeError):
        decide_round(4, 0, True, [], [])  # type: ignore[arg-type]


def test_decide_round_invalid_ci_green() -> None:
    """ci_green must be a bool; a non-bool raises TypeError."""
    with pytest.raises(TypeError):
        decide_round(1, 0, "yes", [], [])  # type: ignore[arg-type]


def test_decide_round_invalid_blockers() -> None:
    """blockers must be int or 'unknown'; another string raises TypeError."""
    with pytest.raises(TypeError):
        decide_round(1, "bad", True, [], [])  # type: ignore[arg-type]


def test_decide_round_invalid_blockers_mixed() -> None:
    """blockers as a bool (not a real int) raises TypeError (bool != int|'unknown')."""
    with pytest.raises(TypeError):
        decide_round(1, True, True, [], [])  # type: ignore[arg-type]
