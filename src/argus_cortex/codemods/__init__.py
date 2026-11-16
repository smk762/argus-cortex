"""libcst codemods that move local code onto cortex's paved roads (ai-code-auditor#56).

Run one over a repo's source, then review the diff::

    uv run --no-project --with 'argus-cortex[codemods]' python -m argus_cortex.codemods fullmatch src/
    uv run --no-project --with 'argus-cortex[codemods]' python -m argus_cortex.codemods read-upload src/ --check

* ``fullmatch``: an anchored id check, ``re.match(r"^...$", v)`` / ``PATTERN.match(v)`` / ``.search``,
  becomes ``fullmatch``. ``$`` also matches before a trailing newline, so ``"id\\n"`` passes today.
  Only patterns that start with ``^`` and end with ``$``, have no top-level ``|`` and aren't
  ``MULTILINE`` are touched. A plain single-segment id check is a candidate for
  :func:`argus_cortex.fs.safe_name`, which the summary names.
  **Review each hit:** a *validator* (an id, a hash, a header value) is what this is for. A
  *parser* matching lines read with their newline (``for line in f``) relied on ``$`` matching
  before it, and ``fullmatch`` would stop matching: strip the line, or leave that call alone.
* ``read-upload``: ``await upload.read()`` on an ``UploadFile`` parameter (or an item of a
  ``list[UploadFile]``) becomes ``await read_upload(upload, MAX_UPLOAD_BYTES)``
  (:mod:`argus_cortex.limits`), adding the import and a ``MAX_UPLOAD_BYTES`` constant when the module
  has none. Mapping ``TooLarge`` to 413 and ``BadInput`` to 400 is left to the caller, and the summary
  says so.

``net`` and ``auth`` have no codemod: their local shapes vary too much to rewrite without changing
behaviour, and the argus-ci suite rules point at the helper instead.

libcst is only imported by the codemods themselves (the ``[codemods]`` extra).
"""

from __future__ import annotations

CODEMODS = ("fullmatch", "read-upload")

__all__ = ["CODEMODS"]
