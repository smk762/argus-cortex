"""``python -m argus_cortex.codemods <codemod> PATH... [--check]``: rewrite files in place, or with
``--check`` print the diff and exit 1 when anything would change."""

from __future__ import annotations

import argparse
import difflib
import sys
from pathlib import Path

from argus_cortex.codemods import CODEMODS

SKIP = {".git", ".venv", "venv", "node_modules", "__pycache__", "build", "dist", ".tox"}


def _files(paths: list[str]) -> list[Path]:
    out: list[Path] = []
    for raw in paths:
        p = Path(raw)
        if p.is_file() and p.suffix == ".py":
            out.append(p)
        elif p.is_dir():
            out += sorted(f for f in p.rglob("*.py") if not SKIP & set(f.parts))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m argus_cortex.codemods", description=__doc__)
    ap.add_argument("codemod", choices=CODEMODS)
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--check", action="store_true", help="print the diff, change nothing, exit 1 if any")
    args = ap.parse_args(argv)

    if args.codemod == "fullmatch":
        from argus_cortex.codemods import fullmatch as mod
    else:
        from argus_cortex.codemods import read_upload as mod

    changed = 0
    for path in _files(args.paths):
        source = path.read_text(encoding="utf-8")
        try:
            new, report = mod.transform(source)
        except Exception as exc:  # a file libcst can't parse is reported, not fatal
            print(f"{path}: skipped ({type(exc).__name__}: {exc})", file=sys.stderr)
            continue
        if new == source:
            continue
        changed += 1
        for line in mod.summary(report):
            print(f"{path}: {line}", file=sys.stderr)
        if args.check:
            sys.stdout.writelines(
                difflib.unified_diff(source.splitlines(True), new.splitlines(True), f"a/{path}", f"b/{path}")
            )
        else:
            path.write_text(new, encoding="utf-8")
    print(f"{args.codemod}: {changed} file(s) {'would change' if args.check else 'changed'}", file=sys.stderr)
    return 1 if args.check and changed else 0


if __name__ == "__main__":
    raise SystemExit(main())
