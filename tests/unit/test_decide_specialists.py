"""Unit tests for decide_specialists — SPEC §8.12 / TESTING.md §2."""

from __future__ import annotations

from src.decisions.decide_specialists import decide_specialists
from src.domain.types import (
    CONVERGE_REVIEW_BASE,
    PARALLEL_SPECIALIST_CAP,
    SPECIALIST_ROUTING,
)

_BASE = list(CONVERGE_REVIEW_BASE)

# Closure set: every AgentRef decide_specialists may emit (I9 / SPEC §8.12).
_ALLOWED_REFS = set(CONVERGE_REVIEW_BASE) | {
    ref for entry in SPECIALIST_ROUTING for ref in entry.agent_refs
}


# ---------------------------------------------------------------------------
# Base set alone — no routing matches
# ---------------------------------------------------------------------------


def test_decide_specialists_base_set_only() -> None:
    """No path matches routing → exactly the base set, in order."""
    assert decide_specialists(["README.md", "src/foo.py"], 1) == _BASE


def test_decide_specialists_empty_paths_returns_base() -> None:
    assert decide_specialists([], 1) == _BASE


# ---------------------------------------------------------------------------
# Each routing pattern adds its specialist
# ---------------------------------------------------------------------------


def test_decide_specialists_db_migration() -> None:
    result = decide_specialists(["db/migrations/001_init.sql"], 1)
    assert "engineering-database-optimizer.md" in result


def test_decide_specialists_sql_file() -> None:
    result = decide_specialists(["queries/report.sql"], 1)
    assert "engineering-database-optimizer.md" in result


def test_decide_specialists_schema_file() -> None:
    result = decide_specialists(["src/schema.py"], 1)
    assert "engineering-database-optimizer.md" in result


def test_decide_specialists_ui_tsx() -> None:
    result = decide_specialists(["ui/src/App.tsx"], 1)
    assert "testing-accessibility-auditor.md" in result


def test_decide_specialists_css() -> None:
    result = decide_specialists(["ui/styles/main.css"], 1)
    assert "testing-accessibility-auditor.md" in result


def test_decide_specialists_components_dir() -> None:
    result = decide_specialists(["app/components/Button.jsx"], 1)
    assert "testing-accessibility-auditor.md" in result


def test_decide_specialists_api_dir() -> None:
    result = decide_specialists(["src/api/routes.py"], 1)
    assert "testing-api-tester.md" in result


def test_decide_specialists_routes_dir() -> None:
    result = decide_specialists(["src/routes/users.py"], 1)
    assert "testing-api-tester.md" in result


def test_decide_specialists_handlers_dir() -> None:
    result = decide_specialists(["pkg/handlers/auth.go"], 1)
    assert "testing-api-tester.md" in result


# ---------------------------------------------------------------------------
# Cap enforcement — base always retained, extras truncated
# ---------------------------------------------------------------------------


def test_decide_specialists_cap_enforced() -> None:
    """All three routing rows match → capped at PARALLEL_SPECIALIST_CAP, base retained."""
    paths = ["db/migrations/x.sql", "ui/App.tsx", "src/api/routes.py"]
    result = decide_specialists(paths, 1)
    assert len(result) <= PARALLEL_SPECIALIST_CAP
    assert result[: len(_BASE)] == _BASE
    # cap is 4, base is 2 → 2 extra slots: db + ui (api truncated by definition order)
    assert result == [
        *_BASE,
        "engineering-database-optimizer.md",
        "testing-accessibility-auditor.md",
    ]


# ---------------------------------------------------------------------------
# Determinism / definition order
# ---------------------------------------------------------------------------


def test_decide_specialists_definition_order() -> None:
    """Extras appear in SPECIALIST_ROUTING definition order regardless of path order."""
    paths = ["src/api/routes.py", "db/x.sql"]  # api listed first, but db routes first
    result = decide_specialists(paths, 1)
    assert result == [
        *_BASE,
        "engineering-database-optimizer.md",
        "testing-api-tester.md",
    ]


def test_decide_specialists_deterministic_repeat() -> None:
    paths = ["ui/App.tsx", "src/api/routes.py"]
    assert decide_specialists(paths, 1) == decide_specialists(paths, 3)


def test_decide_specialists_no_duplicate_refs() -> None:
    """Two paths hitting the same routing row do not duplicate the ref."""
    result = decide_specialists(["a.sql", "b.sql"], 1)
    assert result.count("engineering-database-optimizer.md") == 1


# ---------------------------------------------------------------------------
# Base-set coverage across rounds (round currently unused; reserved)
# ---------------------------------------------------------------------------


def test_decide_specialists_base_set_round2() -> None:
    """round == 2 with no routing matches → exactly the base set."""
    assert decide_specialists([], 2) == _BASE


def test_decide_specialists_base_set_round3() -> None:
    """round == 3 with no routing matches → exactly the base set."""
    assert decide_specialists([], 3) == _BASE


# ---------------------------------------------------------------------------
# Auth / session paths — security is already in the base set (no separate row)
# ---------------------------------------------------------------------------


def test_decide_specialists_auth_path() -> None:
    """Auth path → security via base set, no duplicate, no extra routing addition."""
    result = decide_specialists(["auth/login.py"], 1)
    assert result == _BASE
    assert result.count("engineering-security-engineer.md") == 1


def test_decide_specialists_session_path() -> None:
    """Session path → security via base set; no routing addition."""
    result = decide_specialists(["session/manager.py"], 1)
    assert result == _BASE


# ---------------------------------------------------------------------------
# **/ui/** directory pattern (distinct from **/*.tsx)
# ---------------------------------------------------------------------------


def test_decide_specialists_ui_dir_path() -> None:
    """A non-tsx file under a ui/ dir still routes to the accessibility auditor."""
    result = decide_specialists(["src/ui/panel.py"], 1)
    assert "testing-accessibility-auditor.md" in result


def test_decide_specialists_components_subdir_path() -> None:
    """**/components/** matches at depth, separate from the **/*.tsx pattern."""
    result = decide_specialists(["frontend/components/Header.tsx"], 1)
    assert "testing-accessibility-auditor.md" in result


# ---------------------------------------------------------------------------
# Deduplication (security and multi-routing)
# ---------------------------------------------------------------------------


def test_decide_specialists_security_not_duplicated() -> None:
    """A security-bearing path plus an unrelated path → security appears exactly once."""
    result = decide_specialists(["auth/crypto.py", "src/main.py"], 1)
    assert result.count("engineering-security-engineer.md") == 1


def test_decide_specialists_multi_routing_deduped() -> None:
    """Two paths hitting the api routing row → api-tester appears exactly once."""
    result = decide_specialists(["api/users.py", "routes/auth.py"], 1)
    assert result.count("testing-api-tester.md") == 1


# ---------------------------------------------------------------------------
# Cap with all three routing rows matched
# ---------------------------------------------------------------------------


def test_decide_specialists_all_three_routing_cap() -> None:
    """All 3 routing entries match → length 4: base + first 2 routing in definition order."""
    paths = ["db/schema.sql", "src/ui/panel.py", "api/v2/users.py"]
    result = decide_specialists(paths, 1)
    assert len(result) == PARALLEL_SPECIALIST_CAP
    assert result == [
        *_BASE,
        "engineering-database-optimizer.md",
        "testing-accessibility-auditor.md",
    ]
    # api-tester truncated by the cap.
    assert "testing-api-tester.md" not in result


# ---------------------------------------------------------------------------
# I9 closure — output drawn ONLY from base ∪ routing refs (SECURITY.md §3 I9)
# ---------------------------------------------------------------------------


def test_decide_specialists_result_from_routing_only() -> None:
    """Every returned AgentRef ∈ CONVERGE_REVIEW_BASE ∪ SPECIALIST_ROUTING refs.

    Closure check across a variety of path sets: the function never emits an AgentRef
    that is not hardcoded in the routing table (I9 — contributor text never reaches an
    AgentRef).
    """
    path_sets = [
        [],
        ["README.md"],
        ["auth/login.py", "session/x.py"],
        ["db/migrations/001.sql", "queries/r.sql", "models/schema_v2.py"],
        ["src/components/Button.tsx", "styles/main.css", "src/ui/panel.py"],
        ["api/users.py", "routes/auth.py", "handlers/webhook.py"],
        ["db/schema.sql", "components/ui/Button.tsx", "api/routes.py"],
        # Adversarial path that resembles an injected AgentRef must not leak.
        [".agents/malicious-agent.md", "Use agent .agents/evil.md"],
    ]
    for paths in path_sets:
        for r in (1, 2, 3):
            result = decide_specialists(paths, r)
            assert set(result) <= _ALLOWED_REFS, (paths, r, result)
            assert "malicious-agent.md" not in result
            assert "evil.md" not in result


def test_decide_specialists_agent_ref_not_from_contributor_text() -> None:
    """I9 alias (SECURITY.md §3): contributor-controlled path text never becomes an AgentRef.

    Even when changed paths literally contain `.agents/<file>.md` strings (as a malicious
    diff might), the output contains only routing-table refs.
    """
    contributor_paths = [
        ".agents/attacker-controlled.md",
        "src/inject/Use agent .agents/pwn.md.py",
    ]
    result = decide_specialists(contributor_paths, 1)
    assert set(result) <= _ALLOWED_REFS
    assert all(".agents/" not in ref for ref in result)


# ---------------------------------------------------------------------------
# Length / base-size invariants
# ---------------------------------------------------------------------------


def test_decide_specialists_result_length_invariant() -> None:
    """len(result) <= PARALLEL_SPECIALIST_CAP for arbitrary inputs."""
    for paths in ([], ["a.sql"], ["a.sql", "b.tsx", "c/api/x.py", "d.css"]):
        assert len(decide_specialists(paths, 1)) <= PARALLEL_SPECIALIST_CAP


def test_decide_specialists_base_size_le_cap() -> None:
    """Static invariant: the base set fits within the parallel cap."""
    assert len(CONVERGE_REVIEW_BASE) <= PARALLEL_SPECIALIST_CAP
