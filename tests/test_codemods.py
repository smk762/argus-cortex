"""argus_cortex.codemods: what each rewrites, what it must leave alone, and that the result runs."""

from __future__ import annotations

import re
import textwrap

import pytest

pytest.importorskip("libcst")

from argus_cortex.codemods import fullmatch, read_upload  # noqa: E402
from argus_cortex.codemods.__main__ import main  # noqa: E402


def src(code: str) -> str:
    return textwrap.dedent(code).lstrip()


# ── fullmatch ────────────────────────────────────────────────────────────────


def test_compiled_and_direct_anchored_checks_become_fullmatch() -> None:
    code = src(
        r"""
        import re
        import re as _re

        _SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
        PATTERN = r"^[a-z][a-z0-9_]{0,31}$"
        _SHA = re.compile(PATTERN)


        def ok(v):
            return _SAFE_ID.match(v) and re.match(PATTERN, v) and _re.search(r"^\d+$", v) and _SHA.match(v)
        """
    )
    new, report = fullmatch.transform(code)
    assert new.count(".fullmatch(") == 4 and ".match(" not in new and ".search(" not in new
    assert len(report.rewritten) == 4
    assert report.safe_name == ["line 10:12: ^[A-Za-z0-9][A-Za-z0-9._-]*$"]
    ns: dict = {}
    exec(new, ns)  # noqa: S102
    assert not ns["ok"]("abc\n") and not ns["ok"]("12")


def test_a_function_local_re_import_is_seen() -> None:
    code = src(
        r"""
        SLOT = r"^[a-z][a-z0-9_]{0,31}$"


        def check(slot):
            import re as _re

            if not _re.match(SLOT, slot):
                raise ValueError(slot)
        """
    )
    new, report = fullmatch.transform(code)
    assert "_re.fullmatch(SLOT, slot)" in new and report.review == []


def test_each_rewrite_says_whether_it_only_tightens() -> None:
    code = src(
        r"""
        import re

        ID = re.compile(r"^[a-z]+$")
        MODEL = re.compile(r"^(ckpt|lora)_name$")


        def f(v, key, line):
            if not ID.match(v):
                raise ValueError
            if ID.search(v) is None:
                raise ValueError
            if MODEL.match(key):
                raise ValueError("names a model file")
            return ID.match(line)
        """
    )
    new, report = fullmatch.transform(code)
    assert len(report.rewritten) == 4
    assert [r.split(":")[0] for r in report.review] == ["line 12", "line 14"]  # refuses on match; returned
    lines = fullmatch.summary(report)
    assert sum(x.startswith("tightens") for x in lines) == 2 and sum(x.startswith("REVIEW") for x in lines) == 2


@pytest.mark.parametrize(
    "code",
    [
        'import re\nX = re.compile(r"^a$", re.M)\nX.match(v)\n',  # multiline: $ means end of line
        'import re\nre.match(r"^a$", v, re.MULTILINE | re.I)\n',
        'import re\nre.match(r"(?m)^a$", v)\n',
        'import re\nre.match(r"^a|b$", v)\n',  # top-level alternation changes meaning
        'import re\nre.match(r"^a", v)\n',  # a prefix check, on purpose
        'import re\nre.match(r"^a\\Z", v)\n',  # already exact
        'import re\nX = re.compile(r"^a$")\nX.match(v, 3)\n',  # pos changes the meaning
        "import re\nre.match(pattern_from_config, v)\n",  # unknown pattern
        'import re\nre.match(r"^a$", v, flags)\n',  # unknown flags
        "X.match(v)\n",  # no re import at all
        'import re\n\ndef f():\n    X = re.compile(r"^a$")\n    return X.match(v)\n',  # local, could be rebound
    ],
)
def test_left_alone(code: str) -> None:
    assert fullmatch.transform(code)[0] == code


@pytest.mark.parametrize("pattern", [r"^[a-z]+$", r"^\d{3}-\d{4}$", r"^(a|b)c$", r"^$", r"^[^/]+\.json$"])
@pytest.mark.parametrize(
    "value", ["abc", "abc\n", "123-4567", "123-4567\n", "ac", "bc\n", "", "\n", "x.json", "x.json\n"]
)
def test_fullmatch_equals_match_except_for_the_trailing_newline(pattern: str, value: str) -> None:
    assert fullmatch.anchored(pattern)
    expected = bool(re.match(pattern, value)) and not value.endswith("\n")
    assert bool(re.fullmatch(pattern, value)) == expected


# ── read-upload ──────────────────────────────────────────────────────────────


def test_upload_reads_become_read_upload() -> None:
    code = src(
        """
        from fastapi import FastAPI, File, UploadFile

        app = FastAPI()


        @app.post("/one")
        async def one(file: UploadFile = File(...)):
            data = await file.read()
            return len(data)


        @app.post("/many")
        async def many(files: list[UploadFile] = File(...), note: str | None = None):
            out = [len(await f.read()) for f in files]
            for f in files:
                out.append(len(await f.read()))
            for i, g in enumerate(files):
                out.append(len(await g.read()))
            return out
        """
    )
    new, report = read_upload.transform(code)
    assert len(report.rewritten) == 3 and report.added_cap  # the comprehension's f isn't a loop target
    assert "from argus_cortex.limits import read_upload" in new
    assert new.count("await read_upload(") == 3 and "MAX_UPLOAD_BYTES = 50 * 1024 * 1024" in new
    assert "await f.read()" in new  # inside the comprehension: left for a person
    compile(new, "<codemod>", "exec")


def test_an_existing_cap_and_import_are_reused() -> None:
    code = src(
        """
        from argus_cortex.limits import read_upload
        from fastapi import UploadFile

        MAX_UPLOAD_BYTES = 10

        async def f(up: UploadFile | None):
            return await up.read()
        """
    )
    new, report = read_upload.transform(code)
    assert not report.added_cap and new.count("MAX_UPLOAD_BYTES =") == 1
    assert new.count("import read_upload") == 1 and "await read_upload(up, MAX_UPLOAD_BYTES)" in new


@pytest.mark.parametrize(
    "code",
    [
        "async def f(path):\n    return await path.read()\n",  # not an upload
        "async def f(file: UploadFile):\n    return await file.read(1024)\n",  # already bounded
        "async def f(file: UploadFile):\n    return file.read()\n",  # not awaited (sync file API)
        "async def f(request):\n    return await request.body()\n",  # read_body needs a person
    ],
)
def test_upload_left_alone(code: str) -> None:
    assert read_upload.transform(code)[0] == code


# ── the command ──────────────────────────────────────────────────────────────


def test_check_reports_without_writing_then_rewrites(tmp_path, capsys) -> None:
    f = tmp_path / "pkg" / "ids.py"
    f.parent.mkdir()
    f.write_text('import re\n_ID = re.compile(r"^[a-z]+$")\nok = _ID.match\n\ndef g(v):\n    return _ID.match(v)\n')
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "x.py").write_text('import re\nre.match(r"^a$", v)\n')
    (tmp_path / "broken.py").write_text("def (:\n")
    before = f.read_text()

    assert main(["fullmatch", str(tmp_path), "--check"]) == 1
    out, err = capsys.readouterr()
    assert "+    return _ID.fullmatch(v)" in out and f.read_text() == before
    assert "broken.py: skipped" in err and ".venv" not in out

    assert main(["fullmatch", str(tmp_path)]) == 0
    assert "_ID.fullmatch(v)" in f.read_text() and "ok = _ID.match" in f.read_text()
    assert main(["fullmatch", str(tmp_path), "--check"]) == 0  # idempotent
