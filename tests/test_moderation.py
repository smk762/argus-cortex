"""Moderation clients against fake NudeNet and SingGuard servers.

The fakes speak each server's API (shapes taken from live servers), so the
real urllib path, multipart/JSON encoding and response parsing all run.
"""

from __future__ import annotations

import base64
import io
import json
import subprocess
import sys
import threading
from collections.abc import Iterator
from email.parser import BytesParser
from email.policy import HTTP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from PIL import Image

from argus_cortex.moderation import (
    DEFAULT_EXPOSED_LABELS,
    GuardCheckFailed,
    GuardResult,
    ModerationConfigError,
    NsfwResult,
    NudeNetCheckFailed,
    NudeNetClient,
    SingGuardClient,
    exposed_score,
    nudenet,
    policy_name,
    rule_letter,
    rule_matches,
    singguard,
)

_FAIL = 99  # this red value gets an HTTP 500 from either fake


def _png(red: int, size: tuple[int, int] = (64, 48), mode: str = "RGB") -> bytes:
    buf = io.BytesIO()
    Image.new(mode, size, (red, 0, 0, 255)[: len(mode)]).save(buf, "PNG")
    return buf.getvalue()


def _red(data: bytes) -> int:
    return Image.open(io.BytesIO(data)).convert("RGB").getpixel((0, 0))[0]


class _Fake(BaseHTTPRequestHandler):
    seen: list[dict] = []

    def log_message(self, *args) -> None:  # keep test output quiet
        pass

    def _send(self, status: int, payload: object | None = None) -> None:
        out = json.dumps(payload).encode() if payload is not None else b""
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


# ---------------------------------------------------------------------------
# NudeNet
# ---------------------------------------------------------------------------

_DETECTIONS = {
    10: [],
    20: [
        {"class": "FACE_FEMALE", "score": 0.91, "box": [10, 10, 30, 30]},
        {"class": "BELLY_EXPOSED", "score": 0.4, "box": [10, 50, 30, 20]},
    ],
    30: [
        {"class": "FEMALE_BREAST_EXPOSED", "score": 0.72, "box": [5, 5, 20, 20]},
        {"class": "FEMALE_BREAST_EXPOSED", "score": 0.66, "box": [30, 5, 20, 20]},
        {"class": "FACE_FEMALE", "score": 0.8, "box": [10, 0, 10, 10]},
    ],
}


class _NudeNet(_Fake):
    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers["Content-Length"]))
        msg = BytesParser(policy=HTTP).parsebytes(
            f"Content-Type: {self.headers['Content-Type']}\r\n\r\n".encode() + body
        )
        part = next(msg.iter_parts())
        red = _red(part.get_content())
        type(self).seen.append(
            {"path": self.path, "field": part.get_param("name", header="content-disposition"),
             "filename": part.get_filename(), "auth": self.headers["Authorization"]}
        )  # fmt: skip
        if red == _FAIL:
            self._send(500)
        else:
            self._send(200, {"prediction": [_DETECTIONS.get(red, [])], "success": True})


class _SingGuard(_Fake):
    answers: dict[int, dict] = {}

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        image = Image.open(io.BytesIO(base64.b64decode(body["image_base64"])))
        red = image.convert("RGB").getpixel((0, 0))[0]
        type(self).seen.append(
            {"path": self.path, "size": image.size, "format": image.format,
             **{k: v for k, v in body.items() if k != "image_base64"}}
        )  # fmt: skip
        if red == _FAIL:
            self._send(500)
        else:
            self._send(200, {**_GUARD_ANSWERS.get(red, _GUARD_ANSWERS[10]), "model": "SingGuard-4b", "elapsed_ms": 5})


def _serve(handler: type[_Fake]) -> Iterator[tuple[type[_Fake], str]]:
    handler.seen = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield handler, f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def nudenet_server() -> Iterator[tuple[type[_Fake], str]]:
    yield from _serve(_NudeNet)


@pytest.fixture
def singguard_server() -> Iterator[tuple[type[_Fake], str]]:
    yield from _serve(_SingGuard)


def test_nudenet_url() -> None:
    assert NudeNetClient("http://nudenet:8080/").url == "http://nudenet:8080/infer"
    assert NudeNetClient("https://n.lan/x/infer").url == "https://n.lan/x/infer"
    with pytest.raises(ModerationConfigError) as exc:
        NudeNetClient("ftp://user:secret@nudenet")
    assert "secret" not in str(exc.value)  # callers surface this message
    # A host named like the path is still a host, and a query stays a query.
    assert NudeNetClient("http://infer").url == "http://infer/infer"
    assert NudeNetClient("http://h:8080?token=x").url == "http://h:8080/infer?token=x"


def test_credentials_in_the_url_become_basic_auth(nudenet_server) -> None:
    fake, url = nudenet_server
    NudeNetClient(url.replace("http://", "http://us%40er:p%3Aw@"), timeout=5).check(_png(10))
    assert fake.seen[0]["auth"] == "Basic " + base64.b64encode(b"us@er:p:w").decode()


def test_nudenet_check(nudenet_server) -> None:
    fake, url = nudenet_server
    client = NudeNetClient(url, timeout=5)
    assert client.check(_png(10)) == NsfwResult()
    portrait = client.check(_png(20), 'we"ird\r\nname.png')
    assert portrait.exposed_score == 0.0  # a face and a belly are not exposed nudity
    assert portrait.labels == {"FACE_FEMALE": 0.91, "BELLY_EXPOSED": 0.4}
    topless = client.check(_png(30))
    assert topless.exposed_score == 0.72 and len(topless.detections) == 3
    assert list(topless.labels) == ["FACE_FEMALE", "FEMALE_BREAST_EXPOSED"]  # highest first
    with pytest.raises(NudeNetCheckFailed, match="HTTP 500"):
        client.check(_png(_FAIL))
    assert {s["path"] for s in fake.seen} == {"/infer"} and {s["field"] for s in fake.seen} == {"f1"}
    assert "we_ird__name.png" in {s["filename"] for s in fake.seen}  # header-safe


def test_nudenet_parsing_ignores_malformed_entries() -> None:
    r = nudenet.to_result(
        [
            {"class": "BUTTOCKS_EXPOSED", "score": 0.6, "box": [0, 0, 1, 1]},
            {"class": "NOT_A_CLASS", "score": 0.99, "box": [0, 0, 1, 1]},
            {"class": "ANUS_EXPOSED", "score": "high", "box": [0, 0, 1, 1]},
            {"class": "ANUS_EXPOSED", "score": True, "box": [0, 0, 1, 1]},
            {"class": "ANUS_EXPOSED", "score": 0.9, "box": [0, 0, 1]},
            {"class": "ANUS_EXPOSED", "score": float("nan"), "box": [0, 0, 1, 1]},
            {"class": "ANUS_EXPOSED", "score": 0.9, "box": [0, 0, float("inf"), 1]},
            {"class": "ANUS_EXPOSED", "score": nudenet.MIN_REPORTED_SCORE / 2, "box": [0, 0, 1, 1]},
            {"class": ["ANUS_EXPOSED"], "score": 0.9, "box": [0, 0, 1, 1]},
            {"class": {"x": 1}, "score": 0.9, "box": [0, 0, 1, 1]},
            "not a detection",
        ]
    )
    assert r.labels == {"BUTTOCKS_EXPOSED": 0.6} and r.exposed_score == 0.6


@pytest.mark.parametrize(
    "payload",
    [{"prediction": [[]], "success": False}, {"prediction": [[], []], "success": True}, {"success": True}, ["x"]],
)
def test_nudenet_bad_responses_are_failures(monkeypatch: pytest.MonkeyPatch, payload) -> None:
    monkeypatch.setattr(nudenet, "post_json", lambda *a, **k: payload)
    with pytest.raises(NudeNetCheckFailed):
        NudeNetClient("http://n").check(_png(10))


def test_exposed_set_is_the_five_exposed_nudity_classes() -> None:
    assert set(DEFAULT_EXPOSED_LABELS) == {
        "BUTTOCKS_EXPOSED", "FEMALE_BREAST_EXPOSED", "FEMALE_GENITALIA_EXPOSED", "MALE_GENITALIA_EXPOSED",
        "ANUS_EXPOSED",
    }  # fmt: skip
    labels = {"FEET_EXPOSED": 0.9, "MALE_BREAST_EXPOSED": 0.9, "BUTTOCKS_EXPOSED": 0.3}
    assert exposed_score(labels) == 0.3
    assert exposed_score(labels, ["MALE_BREAST_EXPOSED"]) == 0.9


# ---------------------------------------------------------------------------
# SingGuard
# ---------------------------------------------------------------------------

_CRIME = "B. Real-World Crimes & Public Safety"
_GORE = "C. Unethical Behavior"
_GUARD_ANSWERS: dict[int, dict] = {
    10: {"safe": True, "verdict": "safe", "rule": "Safe", "explanation": "Safe.", "categories": []},
    20: {
        "safe": False,
        "verdict": "unsafe",
        "rule": _CRIME,
        "explanation": f"Triggered rule: {_CRIME}.",
        "categories": [{"code": _CRIME, "label": _CRIME, "why": "-"}, {"code": _GORE, "label": _GORE, "why": "-"}],
    },
    30: {"safe": False, "verdict": "unsafe", "rule": _GORE, "explanation": "", "categories": 5},  # malformed
    40: {"verdict": "safe"},  # no boolean verdict: a failed check, not a pass
}


def test_singguard_config() -> None:
    client = SingGuardClient("http://192.168.1.198:9150/")
    assert client.url == "http://192.168.1.198:9150/api/guard/check"
    assert client.policy is None and client.policy_name == "default" and client.thinking == "fast"
    custom = SingGuardClient("http://g", policy="  ### A. Minors\n")
    assert custom.policy == "### A. Minors" and custom.policy_name == policy_name("### A. Minors")
    assert custom.policy_name == policy_name("  ### A. Minors\n")  # a policy read from a file matches
    assert custom.policy_name.startswith("custom-") and len(custom.policy_name) == len("custom-") + 12
    for kwargs in ({"thinking": "slow"}, {"policy": " \n"}):
        with pytest.raises(ModerationConfigError):
            SingGuardClient("http://g", **kwargs)
    with pytest.raises(ModerationConfigError):
        SingGuardClient("gopher://g")


def test_singguard_check(singguard_server) -> None:
    fake, url = singguard_server
    client = SingGuardClient(url, timeout=5, thinking="fast-slow", policy="### A. Minors")
    safe = client.check(_png(10), text="a cat on a sofa")
    assert safe == GuardResult(safe=True, explanation="Safe.", model="SingGuard-4b", policy=client.policy_name)
    assert client.check(_png(20)).rules == [_CRIME, _GORE]
    gore = client.check(_png(30))
    assert gore.rules == [_GORE] and gore.explanation is None  # no categories: the rule itself
    with pytest.raises(GuardCheckFailed, match="no verdict"):
        client.check(_png(40))
    with pytest.raises(GuardCheckFailed, match="HTTP 500"):
        client.check(_png(_FAIL))
    assert {s["path"] for s in fake.seen} == {"/api/guard/check"}
    assert fake.seen[0]["input"] == "a cat on a sofa" and fake.seen[1]["input"] == ""
    assert {s["thinking"] for s in fake.seen} == {"fast-slow"} and {s["policy"] for s in fake.seen} == {"### A. Minors"}


def test_singguard_default_policy_is_not_sent(singguard_server) -> None:
    fake, url = singguard_server
    SingGuardClient(url).check(_png(10))
    assert "policy" not in fake.seen[0]


def test_singguard_downscales_large_images(singguard_server) -> None:
    fake, url = singguard_server
    SingGuardClient(url).check(_png(10, (4000, 3000)))
    SingGuardClient(url).check(_png(10, (4000, 3000), "RGBA"))
    SingGuardClient(url, downscale=False).check(_png(10, (4000, 3000)))
    assert [(s["size"], s["format"]) for s in fake.seen] == [
        ((1536, 1152), "JPEG"),
        ((1536, 1152), "PNG"),  # alpha kept, not flattened onto black
        ((4000, 3000), "PNG"),
    ]
    small = _png(10)
    assert singguard.downscale(small) is small
    exif = Image.Exif()
    exif[0x0112] = 6  # rotate 90 degrees to view
    buf = io.BytesIO()
    Image.new("RGB", (4000, 3000)).save(buf, "JPEG", exif=exif)
    assert Image.open(io.BytesIO(singguard.downscale(buf.getvalue()))).size == (1152, 1536)  # upright
    with pytest.raises(GuardCheckFailed, match="prepared"):
        singguard.downscale(b"not an image")


def test_downscale_enforces_the_byte_limit() -> None:
    noisy = Image.effect_noise((1200, 900), 100).convert("RGB")
    buf = io.BytesIO()
    noisy.save(buf, "PNG")
    out = singguard.downscale(buf.getvalue(), max_bytes=100_000)
    assert len(out) <= 100_000 and max(Image.open(io.BytesIO(out)).size) < 1200
    with pytest.raises(GuardCheckFailed, match="size limit"):
        singguard.downscale(buf.getvalue(), max_bytes=100)


def test_singguard_without_pillow_fails_at_construction(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(singguard.importlib.util, "find_spec", lambda name: None)
    with pytest.raises(ModerationConfigError, match="images"):
        SingGuardClient("http://g")
    assert SingGuardClient("http://g", downscale=False).downscale is False


@pytest.mark.parametrize(
    ("fired", "wanted", "expected"),
    [(_CRIME, "B", True), (_CRIME, "b.", True), (_CRIME, _CRIME.lower(), True), (_CRIME, "B. Real", False),
     (_CRIME, "C", False), ("Minors", "minors", True)],
)  # fmt: skip
def test_rule_matches(fired: str, wanted: str, expected: bool) -> None:
    assert rule_matches(fired, wanted) is expected


def test_rule_letter() -> None:
    assert rule_letter(_CRIME) == "B" and rule_letter("Minors") == "Minors"


# ---------------------------------------------------------------------------
# Transport and packaging
# ---------------------------------------------------------------------------


def test_proxies_in_the_environment_are_ignored(nudenet_server, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake, url = nudenet_server
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")  # nothing listens there
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    assert NudeNetClient(url, timeout=5).check(_png(10)) == NsfwResult()


def test_unreachable_server_is_a_failed_check() -> None:
    with pytest.raises(NudeNetCheckFailed, match="request failed"):
        NudeNetClient("http://127.0.0.1:9", timeout=2).check(_png(10))


def test_import_does_not_pull_in_pillow() -> None:
    code = "import sys, argus_cortex, argus_cortex.moderation; assert 'PIL' not in sys.modules"
    subprocess.run([sys.executable, "-c", code], check=True)
