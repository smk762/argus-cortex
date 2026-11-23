"""``python -m argus_cortex.settings``: show which suite file is in use and what it sets, or create one.

It prints variable names and where each comes from, never a value.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from argus_cortex.settings import (
    ENV_FILE_VAR,
    SettingsRefused,
    read_suite_file,
    source,
    suite_file,
    suite_file_candidates,
)

TEMPLATE = """\
# Argus suite settings: what every Argus service on this host shares.
# Keep this file owner-only (chmod 600); a group- or world-readable copy is not read.
# A variable set in a service's own environment wins over this file. NAME=value, no interpolation.

# GPU broker (argus-iris). A URL and a token are all a GPU service needs.
# ARGUS_GPU_BROKER_URL=http://iris-host:8120
# ARGUS_GPU_BROKER_TOKEN=

# Peers: where services find each other, as ARGUS_<PEER>_URL (argus_cortex.peers).
# ARGUS_NUDENET_URL=http://retina-host:8130
# ARGUS_SINGGUARD_URL=
# ARGUS_OCR_URL=
# ARGUS_COMFYUI_URL=http://gpu-host:8188
# ARGUS_OLLAMA_URL=http://gpu-host:11434

# Optional stores (argus_cortex.store). Unset: that store is a no-op.
# CORTEX_PG_URL=
# CORTEX_QDRANT_URL=
# CORTEX_S3_ENDPOINT=
# CORTEX_S3_ACCESS_KEY=
# CORTEX_S3_SECRET_KEY=
# CORTEX_S3_BUCKET=
"""


def _init(path: Path) -> int:
    if path.exists():
        print(f"{path} already exists; not overwritten", file=sys.stderr)
        return 1
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Created owner-only from the start, never widened and then narrowed.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(TEMPLATE)
    print(f"created {path} (mode 600)")
    return 0


def _show() -> int:
    candidates = suite_file_candidates()
    if not candidates:
        print(f"suite file: disabled ({ENV_FILE_VAR})")
        return 0
    path = suite_file()
    if path is None:
        print("suite file: none. Looked for:")
        for candidate in candidates:
            print(f"  {candidate}")
        print("Create one with: python -m argus_cortex.settings --init")
        return 0
    try:
        values = read_suite_file(path)
    except (SettingsRefused, OSError) as exc:
        reason = exc if isinstance(exc, SettingsRefused) else type(exc).__name__
        print(f"suite file: {path}\n  NOT READ: {reason}", file=sys.stderr)
        return 1
    print(f"suite file: {path}")
    names = sorted(name for name, value in values.items() if value.strip())
    if not names:
        print("  sets nothing")
    for name in names:
        note = "overridden by this process's environment" if source(name) == "env" else "in use"
        print(f"  {name}: {note}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m argus_cortex.settings", description=__doc__)
    parser.add_argument("--init", action="store_true", help="create the suite file (owner-only) from a template")
    args = parser.parse_args(argv)
    if args.init:
        candidates = suite_file_candidates()
        if not candidates:
            print(f"{ENV_FILE_VAR} disables the suite file", file=sys.stderr)
            return 1
        return _init(candidates[0])
    return _show()


if __name__ == "__main__":
    raise SystemExit(main())
