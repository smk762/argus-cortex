"""Where to send a reader for each package in the suite: its docs page, else PyPI, else its demo, else GitHub.

One table for every place that names a package (dragonhound.dev posts and cards, READMEs, the handbook), so a
link is chosen the same way everywhere and moves to the docs page the day one ships. ``suite_links_json()``
renders the committed ``suite-links.json`` (``make schema`` regenerates it; a test fails when it is stale), which
non-Python consumers read instead of copying the table.

**When a docs page ships** (``docs.dragonhound.dev/packages/<short>/``), set that package's ``docs`` here and
regenerate. A private repo has no entry until it is public. Stdlib only.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

LINKS_VERSION = "1.0"

LinkKind = Literal["docs", "pypi", "demo", "github"]
PRECEDENCE: tuple[LinkKind, ...] = ("docs", "pypi", "demo", "github")
"""The order a link is chosen in: the docs page explains the package, PyPI installs it, the demo shows it, and
GitHub is the source when there is nothing else."""


def _docs(short: str) -> str:
    return f"https://docs.dragonhound.dev/packages/{short}/"


def _pypi(name: str) -> str:
    return f"https://pypi.org/project/{name}/"


def _demo(sub: str) -> str:
    return f"https://{sub}.dragonhound.dev"


def _github(repo: str) -> str:
    return f"https://github.com/smk762/{repo}"


@dataclass(frozen=True)
class PackageLinks:
    """Every link a package has. ``None`` means it has none of that kind yet."""

    name: str
    docs: str | None = None
    pypi: str | None = None
    demo: str | None = None
    github: str | None = None

    def get(self, kind: LinkKind) -> str | None:
        return getattr(self, kind)

    def best(self) -> tuple[LinkKind, str]:
        """The link to use, by :data:`PRECEDENCE`, with its kind."""
        for kind in PRECEDENCE:
            url = self.get(kind)
            if url:
                return kind, url
        raise ValueError(f"{self.name} has no links")  # every entry has at least its repo


def _package(name: str, *, docs: str | None = None, pypi: bool = True, demo: str | None = None) -> PackageLinks:
    return PackageLinks(
        name=name,
        docs=_docs(docs) if docs else None,
        pypi=_pypi(name) if pypi else None,
        demo=_demo(demo) if demo else None,
        github=_github(name),
    )


PACKAGES: dict[str, PackageLinks] = {
    p.name: p
    for p in (
        _package("argus-cortex", docs="cortex"),
        _package("argus-quarry", docs="quarry", demo="quarry"),
        _package("argus-curator", docs="curator", demo="cull"),
        _package("argus-lens", docs="lens", demo="lightbox"),
        _package("argus-forge", docs="forge"),
        _package("argus-proof", docs="proof", demo="proof"),
        _package("argus-atlas", docs="atlas", demo="atlas"),
        _package("argus-darkroom", docs="darkroom", demo="safelight"),
        _package("argus-mint", docs="mint", demo="assay"),
        _package("argus-survey", docs="survey", demo="sightline"),
        _package("argus-mask", docs="mask", demo="mask"),
        _package("argus-puppet", docs="puppet"),
        _package("argus-echo", docs="echo"),
        _package("argus-iris", docs="iris", demo="iris"),
        _package("argus-scout", docs="scout"),
        _package("argus-herald", docs="herald"),
        _package("argus-retina", docs="retina", pypi=False),
        _package("argus-studio", pypi=False, demo="studio"),
        _package("ai-code-auditor", docs="auditor", pypi=False),
    )
}


def link_for(name: str) -> str:
    """The URL to link ``name`` to (its docs page, else PyPI, else its demo, else GitHub).

    Raises ``KeyError`` for a name that isn't in :data:`PACKAGES`: add the package rather than guess a URL.
    """
    return PACKAGES[name].best()[1]


def suite_links_json() -> str:
    """The committed ``suite-links.json``: every package's links and the one to use (``best``)."""
    doc = {
        "links_version": LINKS_VERSION,
        "precedence": list(PRECEDENCE),
        "packages": {
            name: {
                **{kind: p.get(kind) for kind in PRECEDENCE},
                "best": dict(zip(("kind", "url"), p.best(), strict=True)),
            }
            for name, p in PACKAGES.items()
        },
    }
    return json.dumps(doc, indent=2) + "\n"


__all__ = ["LINKS_VERSION", "PACKAGES", "PRECEDENCE", "LinkKind", "PackageLinks", "link_for", "suite_links_json"]
