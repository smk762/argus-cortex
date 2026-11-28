"""The suite's package links: one table, chosen in one order, committed for non-Python readers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from argus_cortex.suite_links import PACKAGES, PRECEDENCE, PackageLinks, link_for, suite_links_json

ROOT = Path(__file__).resolve().parents[1]


def test_committed_json_is_current():
    committed = (ROOT / "suite-links.json").read_text(encoding="utf-8")
    assert committed == suite_links_json(), "suite-links.json is stale: run make schema"


def test_precedence_is_docs_then_pypi_then_demo_then_github():
    assert PRECEDENCE == ("docs", "pypi", "demo", "github")
    full = PackageLinks("x", docs="https://d/", pypi="https://p/", demo="https://m", github="https://g")
    assert full.best() == ("docs", "https://d/")
    assert PackageLinks("x", pypi="https://p/", demo="https://m", github="https://g").best() == ("pypi", "https://p/")
    assert PackageLinks("x", demo="https://m", github="https://g").best() == ("demo", "https://m")
    assert PackageLinks("x", github="https://g").best() == ("github", "https://g")
    with pytest.raises(ValueError, match="no links"):
        PackageLinks("x").best()


def test_known_fallbacks():
    assert link_for("argus-cortex") == "https://docs.dragonhound.dev/packages/cortex/"
    assert link_for("argus-echo") == "https://pypi.org/project/argus-echo/"  # no docs page yet
    assert link_for("argus-studio") == "https://studio.dragonhound.dev"  # not on PyPI
    assert link_for("ai-code-auditor") == "https://github.com/smk762/ai-code-auditor"
    with pytest.raises(KeyError):
        link_for("argus-nonexistent")


def test_every_link_is_https_and_every_package_has_its_repo():
    for name, p in PACKAGES.items():
        assert p.name == name
        assert p.github == f"https://github.com/smk762/{name}"
        for kind in PRECEDENCE:
            url = p.get(kind)
            assert url is None or url.startswith("https://"), (name, kind, url)
        if p.docs:
            assert p.docs.startswith("https://docs.dragonhound.dev/packages/") and p.docs.endswith("/")


def test_json_carries_the_choice():
    doc = json.loads(suite_links_json())
    assert doc["precedence"] == list(PRECEDENCE)
    assert doc["packages"]["argus-iris"]["best"] == {"kind": "pypi", "url": "https://pypi.org/project/argus-iris/"}
    assert set(doc["packages"]) == set(PACKAGES)
