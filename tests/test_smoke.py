from __future__ import annotations

import argus_cortex


def test_version_is_exposed() -> None:
    assert isinstance(argus_cortex.__version__, str)
    assert argus_cortex.__version__
