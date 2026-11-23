"""Shared fixtures."""

from __future__ import annotations

import pytest

from argus_cortex import settings


@pytest.fixture(autouse=True)
def _no_suite_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never read the developer's own suite file; the settings tests name theirs explicitly."""
    monkeypatch.setenv(settings.ENV_FILE_VAR, "off")
