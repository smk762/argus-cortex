"""argus_cortex.fs: hostile inputs as examples, then as properties (hypothesis)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from argus_cortex.fs import PathRefused, is_within, safe_basename, safe_join, safe_name


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """root/ with a file, a subdir, a symlink inside, a symlink out, and a symlink loop."""
    root = tmp_path / "root"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "a.txt").write_text("a")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("s")
    (root / "in_link").symlink_to(root / "sub")
    (root / "out_link").symlink_to(outside)
    (root / "loop1").symlink_to(root / "loop2")
    (root / "loop2").symlink_to(root / "loop1")
    return root


# ── safe_join: examples ──────────────────────────────────────────────────────


@pytest.mark.parametrize("rel", ["sub/a.txt", "sub/../sub/a.txt", "in_link/a.txt", "new/file.png", "./sub"])
def test_paths_under_the_root_are_returned_resolved(tree: Path, rel: str) -> None:
    got = safe_join(tree, rel)
    assert got == (tree / rel).resolve() and got.is_relative_to(tree.resolve())


@pytest.mark.parametrize(
    ("rel", "why"),
    [
        ("..", "outside"),
        ("../outside/secret.txt", "outside"),
        ("sub/../../outside", "outside"),
        ("out_link/secret.txt", "outside"),  # a symlink out: the lexical path looks fine
        ("/etc/passwd", "relative"),
        ("", "empty"),
        ("a\x00b", "NUL"),
        ("..\\..\\outside", "backslash"),
        ("sub\\a.txt", "backslash"),
        (".", "root itself"),
        ("sub/..", "root itself"),
    ],
)
def test_hostile_paths_are_refused(tree: Path, rel: str, why: str) -> None:
    with pytest.raises(PathRefused, match=why):
        safe_join(tree, rel)


@pytest.mark.parametrize(
    "rel", ["loop1", "loop1/x", "loop1/../in_link", "loop1/../out_link", "loop1/../out_link/secret.txt"]
)
def test_a_path_through_a_symlink_loop_is_refused(tree: Path, rel: str) -> None:
    """A loop stops resolution early, leaving the rest of the path unfollowed: ``loop1/../out_link`` came back as
    an unresolved ``out_link`` pointing outside the root. Nothing through a loop is returned."""
    with pytest.raises(PathRefused, match="could not be resolved"):
        safe_join(tree, rel)
    assert not is_within(tree, tree / rel)


def test_tilde_is_not_expanded(tree: Path) -> None:
    assert safe_join(tree, "~/x") == (tree / "~" / "x").resolve()


def test_root_and_absolute_are_opt_in(tree: Path) -> None:
    assert safe_join(tree, ".", allow_root=True) == tree.resolve()
    inside = str((tree / "sub" / "a.txt").resolve())
    assert safe_join(tree, inside, allow_absolute=True) == Path(inside)
    with pytest.raises(PathRefused, match="outside"):
        safe_join(tree, str(tree.parent / "outside"), allow_absolute=True)


def test_message_names_the_label_not_the_value(tree: Path) -> None:
    with pytest.raises(PathRefused) as e:
        safe_join(tree, "../../etc/shadow", label="export path")
    assert str(e.value).startswith("export path:") and "shadow" not in str(e.value)


def test_is_within(tree: Path) -> None:
    assert is_within(tree, tree / "sub" / "a.txt") and is_within(tree, tree)
    assert not is_within(tree, tree / "out_link" / "secret.txt")
    assert not is_within(tree, "\x00")


# ── safe_join: properties ────────────────────────────────────────────────────

# Path pieces an attacker would combine: traversal, separators, symlinks in and out, absolute
# roots, encodings that look like separators, NUL, newlines and plain names.
PIECES = st.sampled_from(
    [
        "..",
        ".",
        "",
        "sub",
        "a.txt",
        "in_link",
        "out_link",
        "loop1",
        "~",
        "%2e%2e",
        "\\",
        "\x00",
        "\n",
        "outside",
        "/",
        "//",
        "...",
        "a b",
        "ü",
    ]
)
PATHS = st.lists(PIECES, max_size=8).map("/".join) | st.text(max_size=40)


@settings(max_examples=400, suppress_health_check=[HealthCheck.function_scoped_fixture], deadline=None)
@given(rel=PATHS, allow_root=st.booleans(), allow_absolute=st.booleans())
def test_anything_returned_is_inside_and_nothing_else_escapes(
    tree: Path, rel: str, allow_root: bool, allow_absolute: bool
) -> None:
    """Whatever the input: either PathRefused, or a resolved path under the root that, followed
    through every symlink, still lands under the root. Never any other exception."""
    try:
        got = safe_join(tree, rel, allow_root=allow_root, allow_absolute=allow_absolute)
    except PathRefused:
        return
    base = tree.resolve()
    assert got == got.resolve()  # already resolved: what was checked is what's used
    assert got.is_relative_to(base)
    assert allow_root or got != base
    assert os.path.realpath(got).startswith(str(base))


# ── safe_name / safe_basename ────────────────────────────────────────────────


@pytest.mark.parametrize("ok", ["job-1", "a", "0f3e.v2", "run_2026-11-16"])
def test_safe_names(ok: str) -> None:
    assert safe_name(ok) == ok


@pytest.mark.parametrize("bad", ["", ".", "..", ".hidden", "a/b", "a\\b", "job\n", "a\x00", "-x", "_x", "x" * 129, "ü"])
def test_unsafe_names(bad: str) -> None:
    with pytest.raises(PathRefused):
        safe_name(bad)


def test_custom_pattern_still_refuses_dot_names() -> None:
    import re

    loose = re.compile(r"[.a-z]+")
    assert safe_name("abc", pattern=loose) == "abc"
    for bad in (".", ".."):
        with pytest.raises(PathRefused):
            safe_name(bad, pattern=loose)


@given(st.text(max_size=60))
def test_accepted_names_are_one_plain_segment(value: str) -> None:
    try:
        got = safe_name(value)
    except PathRefused:
        return
    assert got == value and "/" not in got and "\\" not in got and "\n" not in got and got not in {".", ".."}
    assert Path(got).name == got


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("photo.jpg", "photo.jpg"),
        ("../../etc/passwd", "passwd"),
        ("..\\..\\boot.ini", "boot.ini"),
        ("C:\\Users\\me\\My Photo (1).png", "My Photo (1).png"),
        ("фото.jpg", "фото.jpg"),
        ("  spaced.png  ", "spaced.png"),
        ("dir/", "dir"),
    ],
)
def test_basename_keeps_the_last_component(raw: str, want: str) -> None:
    assert safe_basename(raw) == want


@pytest.mark.parametrize("bad", ["", "..", ".", ".bashrc", "a\x00.png", "evil\n.png", "x/..", "a\x1b[31m.png"])
def test_basename_refuses(bad: str) -> None:
    with pytest.raises(PathRefused):
        safe_basename(bad)


@given(st.text(max_size=80))
def test_basename_is_always_one_harmless_segment(raw: str) -> None:
    try:
        got = safe_basename(raw)
    except PathRefused:
        return
    assert got and got not in {".", ".."} and not got.startswith(".")
    assert "/" not in got and "\\" not in got and "\x00" not in got
    assert all(ch.isprintable() for ch in got)
    assert Path(got).name == got


def test_basename_length_is_counted_in_bytes() -> None:
    assert safe_basename("ф" * 127) == "ф" * 127
    with pytest.raises(PathRefused, match="bytes"):
        safe_basename("ф" * 128)  # 256 bytes in UTF-8: open() would fail with "File name too long"
