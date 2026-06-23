"""Tests for SPA history-mode fallback (SPAStaticFiles).

BrowserRouter (history-mode routing) requires the server to serve index.html
for any client-side route that is not a real file — otherwise a browser
refresh or direct deep-link returns a 404.

The fix: SPAStaticFiles subclass overrides get_response() to catch the 404
that StaticFiles raises for a missing path and return index.html instead.
API routes are registered before the mount and always take precedence.

Tests:
  1. Client routes (/runs, /triage) → 200 + index.html body.
  2. Unknown /api/ sub-path → 404 JSON ({"detail": …}), never index.html.
  3. /healthz → 200 JSON with version/sha.
  4. Real static asset under dist → served verbatim (correct body).
  5. Root / → index.html (existing StaticFiles html=True behaviour preserved).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from src.api.main import SPAStaticFiles, create_app
from src.db.audit import AuditLog
from src.ports.fakes import FakeForgePort, FakeHarnessPort, FakeSessionPort
from src.service.orchestrator import OrchestratorService

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_service() -> OrchestratorService:
    session = FakeSessionPort()
    harness = FakeHarnessPort(session=session)
    forge = FakeForgePort()
    audit = AuditLog()
    return OrchestratorService(
        forge=forge,
        harness=harness,
        session=session,
        audit=audit,
        allowlist=[],
        owner="test",
    )


def _make_dist(tmp_path: Path) -> Path:
    """Build a minimal ui/dist tree with index.html and one real asset."""
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text(
        "<!doctype html><html><head></head><body>SPA</body></html>",
        encoding="utf-8",
    )
    assets = dist / "assets"
    assets.mkdir()
    (assets / "app.js").write_text("console.log('app');", encoding="utf-8")
    return dist


def _make_spa_app(tmp_path: Path) -> tuple[FastAPI, Path]:
    """Build a minimal FastAPI app with SPAStaticFiles mounted at /.

    This directly mounts SPAStaticFiles to test SPA fallback behaviour in
    isolation without depending on ui/dist existing at the real path.  API
    and health routes are NOT included — these tests only exercise the static
    layer.  See test_make_full_app_* for the combined API+SPA tests.
    """
    dist = _make_dist(tmp_path)
    app = FastAPI()
    app.mount("/", SPAStaticFiles(directory=str(dist), html=True), name="static")
    return app, dist


def _make_full_app(tmp_path: Path) -> tuple[object, Path]:
    """Build a full create_app() FastAPI app with the SPA mount wired in.

    create_app() skips the mount when ui_dist doesn't exist; here we call
    create_app() without the static mount and then manually append
    SPAStaticFiles so that all API routes are registered first (preserving
    production ordering) and the SPA layer sits last.
    """
    dist = _make_dist(tmp_path)
    service = _make_service()
    # create_app() will not mount the static files (ui/dist doesn't exist in
    # this worktree).  We get a clean app with all API routes, then append
    # the SPAStaticFiles mount ourselves — same order as production.
    app = create_app(service, lifespan=None, webhook_secret=None)
    app.mount("/", SPAStaticFiles(directory=str(dist), html=True), name="static")  # type: ignore[union-attr]
    return app, dist


# ---------------------------------------------------------------------------
# SPAStaticFiles unit tests (isolated static layer)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_spa_client_route_returns_index_html(tmp_path: Path) -> None:
    """GET /runs (a BrowserRouter client route) returns 200 + index.html body."""
    app, dist = _make_spa_app(tmp_path)
    expected_body = (dist / "index.html").read_text(encoding="utf-8")
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/runs", headers={"Accept": "text/html"})
    assert response.status_code == 200
    assert response.text == expected_body


@pytest.mark.asyncio
async def test_spa_triage_route_returns_index_html(tmp_path: Path) -> None:
    """GET /triage (a BrowserRouter client route) returns 200 + index.html body."""
    app, dist = _make_spa_app(tmp_path)
    expected_body = (dist / "index.html").read_text(encoding="utf-8")
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/triage")
    assert response.status_code == 200
    assert response.text == expected_body


@pytest.mark.asyncio
async def test_spa_root_returns_index_html(tmp_path: Path) -> None:
    """GET / returns 200 index.html (existing html=True behaviour preserved)."""
    app, dist = _make_spa_app(tmp_path)
    expected_body = (dist / "index.html").read_text(encoding="utf-8")
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/")
    assert response.status_code == 200
    assert response.text == expected_body


@pytest.mark.asyncio
async def test_real_asset_served_verbatim(tmp_path: Path) -> None:
    """GET /assets/app.js returns the actual JS file content, not index.html."""
    app, dist = _make_spa_app(tmp_path)
    expected = (dist / "assets" / "app.js").read_text(encoding="utf-8")
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/assets/app.js")
    assert response.status_code == 200
    assert response.text == expected


# ---------------------------------------------------------------------------
# Integration tests — full app: API routes + SPA fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_unknown_route_returns_json_404(tmp_path: Path) -> None:
    """GET /api/<unknown> returns 404 JSON, NOT index.html.

    API routes are registered before the SPA mount so they always take
    precedence — a missing API path yields the FastAPI JSON 404, never HTML.
    """
    app, _ = _make_full_app(tmp_path)
    async with AsyncClient(
        transport=ASGITransport(app=app),  # type: ignore[arg-type]
        base_url="http://test",
    ) as client:
        response = await client.get("/api/does-not-exist")
    assert response.status_code == 404
    body = response.json()
    # Must be JSON dict with detail, not the SPA HTML page
    assert isinstance(body, dict)
    assert "detail" in body


@pytest.mark.asyncio
async def test_healthz_still_works(tmp_path: Path) -> None:
    """/healthz returns 200 JSON with version and sha — not shadowed by SPA mount."""
    app, _ = _make_full_app(tmp_path)
    async with AsyncClient(
        transport=ASGITransport(app=app),  # type: ignore[arg-type]
        base_url="http://test",
    ) as client:
        response = await client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert "version" in body
    assert "sha" in body


@pytest.mark.asyncio
async def test_full_app_client_route_returns_index_html(tmp_path: Path) -> None:
    """With full API+SPA app, GET /runs returns index.html (200), not API 404."""
    app, dist = _make_full_app(tmp_path)
    expected_body = (dist / "index.html").read_text(encoding="utf-8")
    async with AsyncClient(
        transport=ASGITransport(app=app),  # type: ignore[arg-type]
        base_url="http://test",
    ) as client:
        response = await client.get("/runs", headers={"Accept": "text/html"})
    assert response.status_code == 200
    assert response.text == expected_body
