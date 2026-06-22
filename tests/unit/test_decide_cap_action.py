"""Unit tests — decide_cap_action (SPEC §8.4 truth table, full branch coverage).

D3: always escalate; MAX_REDISPATCHES retained as named constant for tests.
"""

from __future__ import annotations

import pytest

from src.decisions.decide_cap_action import MAX_REDISPATCHES, decide_cap_action

# ---------------------------------------------------------------------------
# Row 1 — always escalate (SPEC §8.4)
# ---------------------------------------------------------------------------


def test_decide_cap_action_redispatch_zero_with_issue() -> None:
    """redispatch_count=0, has_issue=True → escalate."""
    assert decide_cap_action(0, True) == "escalate"


def test_decide_cap_action_redispatch_zero_no_issue() -> None:
    """redispatch_count=0, has_issue=False → escalate."""
    assert decide_cap_action(0, False) == "escalate"


def test_decide_cap_action_at_max_with_issue() -> None:
    """redispatch_count=MAX_REDISPATCHES, has_issue=True → escalate."""
    assert decide_cap_action(MAX_REDISPATCHES, True) == "escalate"


def test_decide_cap_action_at_max_no_issue() -> None:
    """redispatch_count=MAX_REDISPATCHES, has_issue=False → escalate."""
    assert decide_cap_action(MAX_REDISPATCHES, False) == "escalate"


def test_decide_cap_action_below_max_with_issue() -> None:
    """redispatch_count=MAX_REDISPATCHES-1, has_issue=True → escalate (D3: no redispatch)."""
    assert decide_cap_action(MAX_REDISPATCHES - 1, True) == "escalate"


def test_decide_cap_action_below_max_no_issue() -> None:
    """redispatch_count=MAX_REDISPATCHES-1, has_issue=False → escalate (D3: no redispatch)."""
    assert decide_cap_action(MAX_REDISPATCHES - 1, False) == "escalate"


def test_decide_cap_action_above_max_with_issue() -> None:
    """redispatch_count > MAX_REDISPATCHES, has_issue=True → escalate."""
    assert decide_cap_action(MAX_REDISPATCHES + 1, True) == "escalate"


def test_decide_cap_action_above_max_no_issue() -> None:
    """redispatch_count > MAX_REDISPATCHES, has_issue=False → escalate."""
    assert decide_cap_action(MAX_REDISPATCHES + 1, False) == "escalate"


def test_decide_cap_action_large_count() -> None:
    """Very large redispatch_count → escalate (D3 is unconditional)."""
    assert decide_cap_action(999, False) == "escalate"


# ---------------------------------------------------------------------------
# MAX_REDISPATCHES constant integrity
# ---------------------------------------------------------------------------


def test_max_redispatches_is_named_constant() -> None:
    """MAX_REDISPATCHES is a named constant (never hardcoded 2 — SPEC §7 / OQ-2)."""
    from src.domain.types import MAX_REDISPATCHES as domain_max

    # The re-exported value must equal the domain constant.
    assert MAX_REDISPATCHES == domain_max


@pytest.mark.parametrize("count", range(MAX_REDISPATCHES + 2))
def test_decide_cap_action_all_counts_escalate(count: int) -> None:
    """All integer redispatch_count values from 0 to MAX_REDISPATCHES+1 → escalate."""
    assert decide_cap_action(count, True) == "escalate"
    assert decide_cap_action(count, False) == "escalate"


# ---------------------------------------------------------------------------
# Usage-error validation (TESTING.md §2.5)
# ---------------------------------------------------------------------------


def test_cap_action_usage_error() -> None:
    """Missing required argument has_issue → TypeError (TESTING.md §2.5)."""
    with pytest.raises(TypeError):
        decide_cap_action(0)  # type: ignore[call-arg]
