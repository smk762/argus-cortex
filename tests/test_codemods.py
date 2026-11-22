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


@pytest.mark.parametrize(
    "rebinding",
    [
        'PAT = re.compile(r"a")',  # reassigned to an unanchored pattern
        "PAT = build()",  # reassigned to something unknown
        'PAT = OTHER = re.compile(r"a")',  # one of several targets
        'PAT, X = re.compile(r"a"), 1',  # a tuple target
        'if (PAT := re.compile(r"a")):\n    pass',  # a walrus
        'def reset():\n    global PAT\n    PAT = re.compile(r"a")',  # rebound from a function
        'for PAT in (re.compile(r"a"),):\n    pass',
    ],
)
@pytest.mark.parametrize("first", [True, False])
def test_a_pattern_name_bound_more_than_once_is_left_alone(rebinding: str, first: bool) -> None:
    """Whichever binding comes last, in whichever branch: the name may not hold the anchored pattern."""
    anchored = 'PAT = re.compile(r"^a$")'
    body = [rebinding, anchored] if first else [anchored, rebinding]
    code = "\n".join(["import re", *body, "def f(v):\n    return PAT.match(v)", ""])
    assert fullmatch.transform(code)[0] == code


def test_a_rebound_pattern_string_is_left_alone() -> None:
    code = 'import re\nP = r"^a$"\nP += "|b"\nX = re.compile(P)\n\ndef f(v):\n    return re.match(P, v) or X.match(v)\n'
    assert fullmatch.transform(code)[0] == code
    branches = 'import re\nif strict:\n    P = r"a"\nelse:\n    P = r"^a$"\nX = re.compile(P)\nX.match(v)\n'
    assert fullmatch.transform(branches)[0] == branches


def test_a_parameter_or_local_that_shadows_a_pattern_is_left_alone() -> None:
    code = 'import re\nPAT = re.compile(r"^a$")\n\ndef f(PAT, v):\n    return PAT.match(v)\n'
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
    assert len(report.rewritten) == 4 and report.added_cap and report.review == []
    assert "from argus_cortex.limits import read_upload" in new
    assert new.count("await read_upload(") == 4 and "MAX_UPLOAD_BYTES = 50 * 1024 * 1024" in new
    assert "[len(await read_upload(f, MAX_UPLOAD_BYTES)) for f in files]" in new  # the comprehension's f too
    assert ".read()" not in new
    compile(new, "<codemod>", "exec")


@pytest.mark.parametrize(
    "annotation",
    [
        "Union[UploadFile, None]",
        "typing.Union[None, UploadFile]",
        "Annotated[UploadFile, File(...)]",
        "Annotated[UploadFile | None, File()]",
        "t.Annotated[Optional[UploadFile], File()]",
    ],
)
def test_every_spelling_of_an_upload_parameter_is_seen(annotation: str) -> None:
    code = f"async def f(up: {annotation}):\n    return await up.read()\n"
    assert "await read_upload(up, MAX_UPLOAD_BYTES)" in read_upload.transform(code)[0]


def test_annotated_metadata_is_not_the_type() -> None:
    code = "async def f(path: Annotated[Reader, UploadFile]):\n    return await path.read()\n"
    assert read_upload.transform(code)[0] == code


@pytest.mark.parametrize(
    "comprehension",
    [
        "[await f.read() for f in files]",
        "{await f.read() for f in files}",
        "{i: await f.read() for i, f in enumerate(files)}",
        "[await f.read() for group in groups for f in files]",
    ],
)
@pytest.mark.parametrize("annotation", ["list[UploadFile]", "Annotated[list[UploadFile], File()]"])
def test_uploads_iterated_in_a_comprehension_are_capped(comprehension: str, annotation: str) -> None:
    code = f"async def f(files: {annotation}, groups):\n    return {comprehension}\n"
    new, report = read_upload.transform(code)
    assert "await read_upload(f, MAX_UPLOAD_BYTES)" in new and ".read()" not in new and len(report.rewritten) == 1


def test_a_nested_function_sees_the_enclosing_uploads() -> None:
    code = src(
        """
        async def endpoint(file: UploadFile, other: UploadFile):
            async def body():
                return await file.read()

            async def shadowed(other):
                return await other.read()

            return await body(), await shadowed(open_stream())
        """
    )
    new, report = read_upload.transform(code)
    assert "return await read_upload(file, MAX_UPLOAD_BYTES)" in new  # the closure's read is capped
    assert "return await other.read()" in new  # the inner parameter is not the upload
    assert report.rewritten == ["line 3: await file.read()"]
    assert report.review == ["line 6: await other.read() (other is not a known UploadFile)"]


@pytest.mark.parametrize(
    "rebinding",
    [
        "file = open_stream()",
        "file: Reader = open_stream()",
        "file += 1",
        "if (file := open_stream()):\n        pass",
        "for file in streams:\n        pass",
        "for i, file in enumerate(streams):\n        pass",
        "async with session.get(url) as file:\n        pass",
        "try:\n        pass\n    except Exception as file:\n        pass",
    ],
)
def test_a_rebound_upload_name_is_no_longer_an_upload(rebinding: str) -> None:
    """After the name is bound to something else, its read is not an upload read: reviewed, never rewritten."""
    code = f"async def f(file: UploadFile, streams):\n    {rebinding}\n    return await file.read()\n"
    new, report = read_upload.transform(code)
    assert (new, report.rewritten) == (code, [])
    assert [r.split(": ", 1)[1] for r in report.review] == ["await file.read() (file is not a known UploadFile)"]


def test_an_upload_reassigned_to_a_file_handle_is_not_rewritten() -> None:
    code = "async def f(file: UploadFile, path):\n    file = open(path, 'rb')\n    return await file.read()\n"
    new, report = read_upload.transform(code)
    assert (new, report.rewritten) == (code, [])


def test_a_read_in_the_rebinding_value_is_still_the_upload() -> None:
    code = "async def f(file: UploadFile):\n    file = await file.read()\n    return file\n"
    new, report = read_upload.transform(code)
    assert "file = await read_upload(file, MAX_UPLOAD_BYTES)" in new and report.rewritten == [
        "line 2: await file.read()"
    ]


def test_an_upload_annotated_local_is_an_upload() -> None:
    code = "async def f(form):\n    file: UploadFile = form['file']\n    return await file.read()\n"
    assert "return await read_upload(file, MAX_UPLOAD_BYTES)" in read_upload.transform(code)[0]


@pytest.mark.parametrize(
    "comprehension",
    ["[f for f in files]", "{f for f in files}", "{i: f for i, f in enumerate(files)}", "list(f for f in files)"],
)
def test_comprehension_targets_do_not_outlive_the_comprehension(comprehension: str) -> None:
    code = f"async def g(files: list[UploadFile]):\n    all_ = {comprehension}\n    f = open_stream()\n    return await f.read()\n"
    code2 = f"async def g(files: list[UploadFile], f):\n    all_ = {comprehension}\n    return await f.read()\n"
    results = [read_upload.transform(c) for c in (code, code2)]
    assert [(new, report.rewritten) for new, report in results] == [(code, []), (code2, [])]


def test_a_comprehension_target_shadows_an_upload_only_inside_it() -> None:
    code = "async def g(f: UploadFile, others):\n    xs = [await f.read() for f in others]\n    return await f.read()\n"
    new, report = read_upload.transform(code)
    assert report.rewritten == ["line 3: await f.read()"]
    assert report.review == ["line 2: await f.read() (f is not a known UploadFile)"]


def test_reads_it_cannot_rewrite_are_listed_for_review(tmp_path, capsys) -> None:
    code = "async def f(files: list[UploadFile]):\n    return await asyncio.gather(*(f.read() for f in files))\n"
    new, report = read_upload.transform(code)
    assert new == code and report.review == ["line 2: f.read() is an upload read with no await of its own"]
    (tmp_path / "app.py").write_text(code)
    assert main(["read-upload", str(tmp_path)]) == 0  # nothing changed, but the summary still names the read
    assert "app.py: REVIEW, left as it was: line 2: f.read()" in capsys.readouterr().err


def test_the_cap_goes_below_the_docstring_when_there_is_no_top_level_import() -> None:
    """transform() always adds a top-level import first, so this is _with_cap's own guarantee."""
    import libcst as cst

    module = cst.parse_module('"""The upload routes."""\n\n\nasync def f(up):\n    return await up.read()\n')
    new = read_upload._with_cap(module).code
    assert new.startswith('"""The upload routes."""\n\nMAX_UPLOAD_BYTES = ')
    ns: dict = {}
    exec(new, ns)  # noqa: S102
    assert ns["__doc__"] == "The upload routes."
    bare = read_upload._with_cap(cst.parse_module("x = 1\n")).code  # no docstring, no import: first
    assert bare.startswith("\nMAX_UPLOAD_BYTES = ") and bare.endswith("x = 1\n")


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


_CHECK = 'import re\n\ndef g(v):\n    return re.match(r"^a$", v)\n'


def test_a_file_that_is_not_utf8_is_skipped_and_the_run_goes_on(tmp_path, capsys) -> None:
    (tmp_path / "a.py").write_text(_CHECK)
    legacy = tmp_path / "b_latin1.py"
    legacy.write_bytes('# caf\xe9\nimport re\nre.match(r"^a$", v)\n'.encode("latin-1"))
    (tmp_path / "c.py").write_text(_CHECK)
    before = legacy.read_bytes()

    assert main(["fullmatch", str(tmp_path)]) == 0
    err = capsys.readouterr().err
    assert "b_latin1.py: skipped (UnicodeDecodeError" in err and "fullmatch: 2 file(s) changed" in err
    assert "re.fullmatch" in (tmp_path / "a.py").read_text() and "re.fullmatch" in (tmp_path / "c.py").read_text()
    assert legacy.read_bytes() == before


@pytest.mark.parametrize("newline", ["\r\n", "\n"])
def test_a_rewrite_keeps_the_files_line_endings(tmp_path, capsys, newline: str) -> None:
    f = tmp_path / "ids.py"
    f.write_bytes(_CHECK.replace("\n", newline).encode())
    assert main(["fullmatch", str(f), "--check"]) == 1
    assert capsys.readouterr().out.count("\n+") == 2  # the header and the one line that changes, not every line
    assert main(["fullmatch", str(f)]) == 0
    assert f.read_bytes() == _CHECK.replace(".match(", ".fullmatch(").replace("\n", newline).encode()
