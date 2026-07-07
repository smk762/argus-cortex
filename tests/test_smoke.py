from __future__ import annotations

import argus_core


def test_version_is_exposed() -> None:
    assert isinstance(argus_core.__version__, str)
    assert argus_core.__version__
