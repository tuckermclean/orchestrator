"""Route entry decision function — pure, synchronous."""

from __future__ import annotations

from dataclasses import dataclass

from src.domain.types import ADJUDICATION_MODEL, DEFAULT_SWARM_MODEL


@dataclass(frozen=True)
class RouteEntryResult:
    model: str
    max_turns: int
    contract: str


_CONTRACT = "agents/orchestrator.md"


def route_entry(event: str) -> RouteEntryResult:
    """Return routing parameters for a forge event.

    Pure and synchronous — never raises for any input.
    """
    if event == "issues":
        return RouteEntryResult(
            model=ADJUDICATION_MODEL,
            max_turns=40,
            contract=_CONTRACT,
        )
    # issue_comment and pull_request_review_comment get sonnet/30
    # Unknown / empty events also default to sonnet/30
    return RouteEntryResult(
        model=DEFAULT_SWARM_MODEL,
        max_turns=30,
        contract=_CONTRACT,
    )
