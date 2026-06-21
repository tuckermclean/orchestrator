"""Shared pytest fixtures."""

from __future__ import annotations

import pytest

from src.ports.fakes import FakeForgePort, FakeHarnessPort, FakeSessionPort


@pytest.fixture
def forge_port() -> FakeForgePort:
    return FakeForgePort()


@pytest.fixture
def harness_port() -> FakeHarnessPort:
    return FakeHarnessPort()


@pytest.fixture
def session_port() -> FakeSessionPort:
    return FakeSessionPort()
