"""libcst codemods that move local code onto cortex's paved roads (ai-code-auditor#56).

Run one over a repo's source, then review the diff::

    uv run --no-project --with 'argus-cortex[codemods]' python -m argus_cortex.codemods fullmatch src/
    uv run --no-project --with 'argus-cortex[codemods]' python -m argus_cortex.codemods read-upload src/ --check

* ``fullmatch``: an anchored id check, ``re.match(r"^...$", v)`` / ``PATTERN.match(v)`` / ``.search``,
  becomes ``fullmatch``. ``$`` also matches before a trailing newline, so ``"id\\n"`` passes today.
  Only patterns that start with ``^`` and end with ``$``, have no top-level ``|`` and aren't
  ``MULTILINE`` are touched. A plain single-segment id check is a candidate for
  :func:`argus_cortex.fs.safe_name`, which the summary names.
  Each rewrite is reported as **tightens** (``not X.match(v)`` / ``X.match(v) is None``: a
  non-match is refused, so ``fullmatch`` only refuses more) or **REVIEW** (anything else). Revert a
  REVIEW hit to ``.match`` when it *refuses on a match* (``fullmatch`` would loosen it) or parses
  lines that keep their newline (``fullmatch`` would stop matching them).
  Not handled: patterns imported from another module, f-string patterns, and any pattern name the
  file binds more than once (it may hold another pattern where it is used). Run the formatter after.
* ``read-upload``: ``await upload.read()`` on an ``UploadFile`` parameter (or an item of a
  ``list[UploadFile]``, in a loop or a comprehension) becomes ``await read_upload(upload, MAX_UPLOAD_BYTES)``
  (:mod:`argus_cortex.limits`), adding the import and a ``MAX_UPLOAD_BYTES`` constant when the module
  has none. Mapping ``TooLarge`` to 413 and ``BadInput`` to 400 is left to the caller, and the summary
  says so. A read it leaves alone is listed as **REVIEW**: ``await x.read()`` where ``x`` isn't a known
  upload (including one a surrounding loop rebinds anywhere in its body, which the read sees from the second pass),
  and an upload's ``read()`` with no ``await`` of its own (``asyncio.gather(*(f.read() for f in files))``).

``net`` and ``auth`` have no codemod: their local shapes vary too much to rewrite without changing
behaviour, and the argus-ci suite rules point at the helper instead.

libcst is only imported by the codemods themselves (the ``[codemods]`` extra).
"""

from __future__ import annotations

CODEMODS = ("fullmatch", "read-upload")

__all__ = ["CODEMODS"]
