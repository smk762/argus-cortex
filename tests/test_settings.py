"""The suite settings file: owner-only or not read, the environment wins, and no value is ever printed."""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

import pytest

from argus_cortex import settings
from argus_cortex.gpu import coordinator_from_env
from argus_cortex.server import env_flag
from argus_cortex.settings.__main__ import main
from argus_cortex.store.config import StoreConfig

SECRET = "s3cret-value-never-printed"
posix_only = pytest.mark.skipif(os.name != "posix", reason="file modes are POSIX")


@pytest.fixture
def suite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An owner-only suite file named by ARGUS_ENV_FILE; tests write its contents."""
    path = tmp_path / "suite.env"
    path.write_text("", encoding="utf-8")
    path.chmod(0o600)
    monkeypatch.setenv(settings.ENV_FILE_VAR, str(path))
    return path


def test_parse_env() -> None:
    text = (
        "# a comment\n\nA=1\nexport B = two words \nC='quoted # kept'\nD=\"x\"\nE=bare # trailing comment\n"
        "F=\nA=last wins\nnot a pair\n1BAD=x\nG=a=b\n"
    )
    values, bad = settings.parse_env(text)
    assert values == {
        "A": "last wins",
        "B": "two words",
        "C": "quoted # kept",
        "D": "x",
        "E": "bare",
        "F": "",
        "G": "a=b",
    }
    assert bad == [10, 11]


def test_values_are_literal_no_interpolation() -> None:
    values, _ = settings.parse_env("A=$HOME/${USER}\nB=`id`\n")
    assert values == {"A": "$HOME/${USER}", "B": "`id`"}


def test_the_environment_wins_and_blank_falls_through(suite: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    suite.write_text("ARGUS_X=from-file\nARGUS_Y=from-file\nARGUS_BLANK=\n", encoding="utf-8")
    monkeypatch.setenv("ARGUS_X", "from-env")
    monkeypatch.setenv("ARGUS_Y", "  ")  # what compose's ${ARGUS_Y:-} leaves behind
    assert settings.get("ARGUS_X") == "from-env" and settings.source("ARGUS_X") == "env"
    assert settings.get("ARGUS_Y") == "from-file" and settings.source("ARGUS_Y") == "file"
    assert settings.get("ARGUS_BLANK") is None and settings.get("ARGUS_BLANK", "d") == "d"
    assert settings.get("ARGUS_MISSING") is None and settings.source("ARGUS_MISSING") is None
    assert settings.environ["ARGUS_Y"] == "from-file" and "ARGUS_BLANK" not in settings.environ


def test_no_file_is_no_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv(settings.ENV_FILE_VAR, "off")
    assert settings.suite_file_candidates() == () and settings.suite_settings() == {}
    monkeypatch.delenv(settings.ENV_FILE_VAR)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert settings.suite_file_candidates()[0] == tmp_path / "argus" / "suite.env"
    with caplog.at_level(logging.WARNING):
        assert settings.get("ARGUS_X") is None
    assert not caplog.records  # a default location that isn't there is silent


def test_a_named_file_that_is_missing_warns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    monkeypatch.setenv(settings.ENV_FILE_VAR, str(tmp_path / "nope.env"))
    with caplog.at_level(logging.WARNING):
        assert settings.suite_settings() == {}
    assert "does not exist" in caplog.text


@posix_only
@pytest.mark.parametrize("mode", [0o640, 0o604, 0o644, 0o660, 0o602, 0o666])
def test_a_file_others_can_read_or_write_is_not_read(suite: Path, mode: int, caplog: pytest.LogCaptureFixture) -> None:
    suite.write_text(f"ARGUS_T_TOKEN={SECRET}\n", encoding="utf-8")
    suite.chmod(mode)
    with pytest.raises(settings.SettingsRefused, match="chmod 600"):
        settings.read_suite_file(suite)
    with caplog.at_level(logging.WARNING):
        assert settings.get("ARGUS_T_TOKEN") is None
    assert "not read" in caplog.text and SECRET not in caplog.text


@posix_only
def test_root_owned_read_only_mount_is_accepted_but_never_a_writable_one(suite: Path, monkeypatch) -> None:
    """A root-owned 0444 file is how a container secret is mounted. Group- or world-writable is refused for anyone."""
    real = os.fstat

    def as_root(mode: int):
        def fake(fd: int):
            st = real(fd)
            return os.stat_result((0o100000 | mode, *st[1:4], 0, *st[5:]))

        return fake

    suite.write_text("ARGUS_X=1\n", encoding="utf-8")
    monkeypatch.setattr(os, "fstat", as_root(0o444))
    assert settings.read_suite_file(suite) == {"ARGUS_X": "1"}
    monkeypatch.setattr(os, "fstat", as_root(0o664))
    with pytest.raises(settings.SettingsRefused, match="writable"):
        settings.read_suite_file(suite)


@posix_only
def test_a_file_owned_by_another_user_is_not_read(suite: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    suite.write_text("ARGUS_X=1\n", encoding="utf-8")
    monkeypatch.setattr(os, "geteuid", lambda: os.stat(suite).st_uid + 1)
    with pytest.raises(settings.SettingsRefused, match="another user"):
        settings.read_suite_file(suite)


def test_oversized_binary_or_non_file_is_refused(suite: Path, tmp_path: Path) -> None:
    suite.write_bytes(b"A=" + b"x" * settings.MAX_FILE_BYTES)
    with pytest.raises(settings.SettingsRefused, match="larger"):
        settings.read_suite_file(suite)
    suite.write_bytes(b"A=\xff\xfe\n")
    with pytest.raises(settings.SettingsRefused, match="UTF-8"):
        settings.read_suite_file(suite)
    with pytest.raises((settings.SettingsRefused, OSError)):
        settings.read_suite_file(tmp_path)


def test_a_bad_line_is_reported_by_number_never_by_content(suite: Path, caplog: pytest.LogCaptureFixture) -> None:
    suite.write_text(f"GOOD=1\n{SECRET}\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        assert settings.suite_settings() == {"GOOD": "1"}
    assert (
        any(re.search(r"lines that are not NAME=value: 2$", m) for m in caplog.messages) and SECRET not in caplog.text
    )


def test_a_changed_file_is_re_read(suite: Path) -> None:
    suite.write_text("ARGUS_TOKEN=one\n", encoding="utf-8")
    assert settings.get("ARGUS_TOKEN") == "one"
    suite.write_text("ARGUS_TOKEN=rotated\n", encoding="utf-8")
    os.utime(suite, ns=(1, 1))  # same size could hide the change on a coarse clock; the mtime moves
    assert settings.get("ARGUS_TOKEN") == "rotated"


def test_apply_fills_only_what_is_unset_and_never_loader_variables(suite: Path, monkeypatch, caplog) -> None:
    suite.write_text(
        "HF_TOKEN=hf\nARGUS_KEEP=file\nARGUS_SKIP=x\nLD_PRELOAD=/tmp/evil.so\nPYTHONPATH=/tmp\nPATH=/tmp\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HF_TOKEN", "x")  # records how it was (delenv alone does not, if unset), to restore it after
    for name in ("HF_TOKEN", "ARGUS_SKIP", "LD_PRELOAD", "PYTHONPATH"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ARGUS_KEEP", "env")
    path_before = os.environ["PATH"]
    with caplog.at_level(logging.WARNING):
        assert settings.apply(names={"HF_TOKEN", "ARGUS_KEEP", "LD_PRELOAD", "PYTHONPATH", "PATH"}) == ["HF_TOKEN"]
    assert os.environ["HF_TOKEN"] == "hf" and os.environ["ARGUS_KEEP"] == "env"
    assert "ARGUS_SKIP" not in os.environ and "LD_PRELOAD" not in os.environ and "PYTHONPATH" not in os.environ
    assert os.environ["PATH"] == path_before and "LD_PRELOAD" in caplog.text


def test_cortex_reads_its_own_settings_from_the_file(suite: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A service gets the broker, the stores and its flags from the suite file with no code change."""
    suite.write_text(
        "ARGUS_GPU_BROKER_URL=http://iris.test:8120\nARGUS_GPU_BROKER_TOKEN=tok\nARGUS_GPU_TENANT=t\n"
        "CORTEX_QDRANT_URL=http://q.test:6333\nARGUS_T_READ_ONLY=yes\n",
        encoding="utf-8",
    )
    for name in ("ARGUS_GPU_COORDINATOR", "ARGUS_GPU_BROKER_URL", "ARGUS_GPU_BROKER_TOKEN", "CORTEX_QDRANT_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(suite.parent)  # no gpu-tenant.yaml here
    coordinator = coordinator_from_env()
    assert type(coordinator).__name__ == "BrokerCoordinator"
    assert StoreConfig.from_env().qdrant_url == "http://q.test:6333"
    assert env_flag("ARGUS_T_READ_ONLY")
    monkeypatch.setenv("ARGUS_GPU_COORDINATOR", "none")  # one service opts out of the suite's broker
    assert type(coordinator_from_env()).__name__ != "BrokerCoordinator"


def test_show_names_variables_and_never_a_value(suite: Path, monkeypatch, capsys: pytest.CaptureFixture[str]) -> None:
    suite.write_text(f"ARGUS_T_TOKEN={SECRET}\nARGUS_T_URL=http://u:{SECRET}@h\n", encoding="utf-8")
    monkeypatch.setenv("ARGUS_T_URL", "http://elsewhere")
    assert main([]) == 0
    out = capsys.readouterr()
    assert "ARGUS_T_TOKEN: in use" in out.out and "ARGUS_T_URL: overridden" in out.out
    assert SECRET not in out.out + out.err


@posix_only
def test_show_reports_a_refused_file_and_init_creates_an_owner_only_one(tmp_path: Path, monkeypatch, capsys) -> None:
    path = tmp_path / "conf" / "suite.env"
    monkeypatch.setenv(settings.ENV_FILE_VAR, str(path))
    assert main(["--init"]) == 0
    assert path.stat().st_mode & 0o777 == 0o600 and settings.read_suite_file(path) == {}
    assert main(["--init"]) == 1  # never overwrites
    path.chmod(0o644)
    assert main([]) == 1 and "NOT READ" in capsys.readouterr().err
