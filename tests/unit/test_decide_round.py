"""Unit tests for decide_round — SPEC §8.3 / TESTING.md §2.

Updated for the 3-tier model (SPEC §5/§8.3/§251): the old ``approve`` token is now
``adjudicate`` (proceed to adjudication phase — NOT final approval).  The adjudicator
(Opus) makes the terminal ship/no-ship judgment.
"""

from __future__ import annotations

import pytest

from src.decisions.decide_round import decide_round

# ---------------------------------------------------------------------------
# Row 1 — adjudicate (spotless): blockers == 0 AND ci_green AND suggestions == 0
# Row 1b — adjudicate (R3): blockers == 0 AND ci_green (suggestions may remain)
# ---------------------------------------------------------------------------


@pytest.mark.covers("§8.3", "row-1-adjudicate")
def test_decide_round_adjudicate_r1_spotless() -> None:
    """Spotless at R1 (0 blockers, 0 suggestions, CI green) → adjudicate."""
    assert decide_round(1, 0, True, [], [], suggestions=0) == "adjudicate"


@pytest.mark.covers("§8.3", "row-1-adjudicate")
def test_decide_round_adjudicate_r2_spotless() -> None:
    """Spotless at R2 → adjudicate (early-exit, skip R3)."""
    assert decide_round(2, 0, True, [], [], suggestions=0) == "adjudicate"


@pytest.mark.covers("§8.3", "row-1-adjudicate")
def test_decide_round_adjudicate_r3_spotless() -> None:
    """Spotless at R3 → adjudicate."""
    assert decide_round(3, 0, True, [], [], suggestions=0) == "adjudicate"


@pytest.mark.covers("§8.3", "row-1b-adjudicate-r3")
def test_decide_round_adjudicate_r3_with_residual_suggestions() -> None:
    """R3 with 0 blockers, CI green, but residual suggestions → adjudicate (row 1b).

    Suggestions may remain at R3 — the nitpicker handles them in the adjudication phase.
    """
    assert decide_round(3, 0, True, [], [], suggestions=2) == "adjudicate"


@pytest.mark.covers("§8.3", "row-1-adjudicate")
def test_decide_round_zero_blockers_ci_red_not_adjudicate_r1() -> None:
    """CI-green guard: 0 blockers but CI red → R1 falls through to fix, not adjudicate."""
    assert decide_round(1, 0, False, [], []) == "fix"


@pytest.mark.covers("§8.3", "row-1-adjudicate")
def test_decide_round_unknown_never_adjudicates_even_ci_green() -> None:
    """'unknown' blockers never produce adjudicate (row 1 needs integer 0)."""
    assert decide_round(3, "unknown", True, [], []) == "escalate:no-verdict"


@pytest.mark.covers("§8.3", "row-1-adjudicate")
def test_decide_round_r1_suggestions_present_not_spotless() -> None:
    """R1 with suggestions > 0 is NOT spotless (row 1 requires suggestions == 0)."""
    assert decide_round(1, 0, True, [], [], suggestions=1) == "fix"


@pytest.mark.covers("§8.3", "row-1-adjudicate")
def test_decide_round_r2_suggestions_present_not_spotless() -> None:
    """R2 with suggestions > 0 is NOT spotless (row 1 requires suggestions==0).

    Row 1b requires round==3. So R2 with suggestions falls to row 4 (fix).
    """
    # Row 1: blockers==0, ci_green, suggestions==1 → NO (suggestions!=0)
    # Row 1b: blockers==0, ci_green → NO (round!=3)
    # Row 2: round==1 → NO
    # Row 3: curr_sigs==prev_sigs==[] → NO (empty)
    # Row 4: round==2 → fix
    assert decide_round(2, 0, True, [], [], suggestions=1) == "fix"


# ---------------------------------------------------------------------------
# Row 2 — R1 always fix (when not approve)
# ---------------------------------------------------------------------------


@pytest.mark.covers("§8.3", "row-2-r1-fix")
def test_decide_round_fix_r1_with_blockers() -> None:
    assert decide_round(1, 2, False, [], ["a:b"]) == "fix"


@pytest.mark.covers("§8.3", "row-2-r1-fix")
def test_decide_round_fix_r1_unknown() -> None:
    """R1 unknown blockers fall through to fix (not approve)."""
    assert decide_round(1, "unknown", True, [], []) == "fix"


# ---------------------------------------------------------------------------
# Row 3 — no-progress: same non-empty sigs two consecutive rounds
# ---------------------------------------------------------------------------


@pytest.mark.covers("§8.3", "row-3-no-progress")
def test_decide_round_no_progress_r2() -> None:
    assert decide_round(2, 1, False, ["a:x"], ["a:x"]) == "escalate:no-progress"


@pytest.mark.covers("§8.3", "row-3-no-progress")
def test_decide_round_no_progress_r3() -> None:
    assert decide_round(3, 1, False, ["a:x"], ["a:x"]) == "escalate:no-progress"


@pytest.mark.covers("§8.3", "row-3-no-progress")
def test_decide_round_no_progress_sorts_before_compare() -> None:
    """Lexicographic sort: out-of-order identical sets still match."""
    assert (
        decide_round(2, 2, False, ["b:y", "a:x"], ["a:x", "b:y"])
        == "escalate:no-progress"
    )


@pytest.mark.covers("§8.3", "row-3-no-progress")
def test_decide_round_empty_sigs_not_no_progress() -> None:
    """prev==curr==[] is NOT no-progress (row 3 needs non-empty curr_sigs)."""
    assert decide_round(2, 1, False, [], []) == "fix"


@pytest.mark.covers("§8.3", "row-3-no-progress")
def test_decide_round_sentinel_normalized_not_no_progress() -> None:
    """Both sides sentinel → normalized to [] → not no-progress."""
    sentinel = ["verdict-file-not-written"]
    assert decide_round(2, 1, False, sentinel, sentinel) == "fix"


@pytest.mark.covers("§8.3", "row-3-no-progress")
def test_decide_round_no_progress_skipped_when_blockers_zero() -> None:
    """blockers==0 (CI red) with stable sigs: row 3 excludes 0 → R2 fix."""
    assert decide_round(2, 0, False, ["a:x"], ["a:x"]) == "fix"


# ---------------------------------------------------------------------------
# Row 4 — R2 fix (when not approve / not no-progress)
# ---------------------------------------------------------------------------


@pytest.mark.covers("§8.3", "row-4-r2-fix")
def test_decide_round_fix_r2_changed_sigs() -> None:
    assert decide_round(2, 1, False, ["a:x"], ["a:y"]) == "fix"


# ---------------------------------------------------------------------------
# Rows 5–7 — R3 terminal outcomes
# ---------------------------------------------------------------------------


@pytest.mark.covers("§8.3", "row-5-r3-no-verdict")
def test_decide_round_r3_no_verdict() -> None:
    assert decide_round(3, "unknown", False, [], []) == "escalate:no-verdict"


@pytest.mark.covers("§8.3", "row-6-r3-ci-red")
def test_decide_round_r3_ci_red() -> None:
    """R3, blockers cleared but CI not green → ci-red."""
    assert decide_round(3, 0, False, [], []) == "escalate:ci-red"


@pytest.mark.covers("§8.3", "row-7-r3-cap-reached")
def test_decide_round_r3_cap_reached() -> None:
    assert decide_round(3, 2, False, ["a:x"], ["a:y"]) == "escalate:cap-reached"


@pytest.mark.covers("§8.3", "row-3-no-progress")
def test_decide_round_r3_no_progress_fires_before_cap() -> None:
    """Row 3 fires before rows 5–7 in R3 when sigs are stable."""
    assert decide_round(3, 2, False, ["a:x"], ["a:x"]) == "escalate:no-progress"


# ---------------------------------------------------------------------------
# Runtime validation — Literal[1,2,3] / bool / int|"unknown" enforced (TESTING.md §2.4)
# ---------------------------------------------------------------------------


@pytest.mark.covers("§8.3", "runtime-validation")
def test_decide_round_invalid_round_zero() -> None:
    """round == 0 is outside Literal[1,2,3] → TypeError (SPEC §8.3)."""
    with pytest.raises(TypeError):
        decide_round(0, 0, True, [], [])  # type: ignore[arg-type]


@pytest.mark.covers("§8.3", "runtime-validation")
def test_decide_round_invalid_round_four() -> None:
    """round == 4 is outside Literal[1,2,3] → TypeError (must not fall through to R3)."""
    with pytest.raises(TypeError):
        decide_round(4, 0, True, [], [])  # type: ignore[arg-type]


@pytest.mark.covers("§8.3", "runtime-validation")
def test_decide_round_invalid_ci_green() -> None:
    """ci_green must be a bool; a non-bool raises TypeError."""
    with pytest.raises(TypeError):
        decide_round(1, 0, "yes", [], [])  # type: ignore[arg-type]


@pytest.mark.covers("§8.3", "runtime-validation")
def test_decide_round_invalid_blockers() -> None:
    """blockers must be int or 'unknown'; another string raises TypeError."""
    with pytest.raises(TypeError):
        decide_round(1, "bad", True, [], [])  # type: ignore[arg-type]


@pytest.mark.covers("§8.3", "runtime-validation")
def test_decide_round_invalid_blockers_mixed() -> None:
    """blockers as a bool (not a real int) raises TypeError (bool != int|'unknown')."""
    with pytest.raises(TypeError):
        decide_round(1, True, True, [], [])  # type: ignore[arg-type]
