"""Moderation clients against fake NudeNet, SingGuard and OCR servers.

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
    CONTENT_RATINGS,
    DEFAULT_EXPOSED_LABELS,
    SUGGESTIVE_LABELS,
    GuardCheckFailed,
    GuardResult,
    ModerationConfigError,
    NsfwResult,
    NudeNetCheckFailed,
    NudeNetClient,
    OcrCheckFailed,
    OcrClient,
    OcrInfo,
    OcrResult,
    SingGuardClient,
    exposed_score,
    normalize_text,
    nudenet,
    ocr,
    policy_name,
    rate_content,
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
    health: dict | None = None  # a test's GET /health answer; None: the route is missing (upstream 320n)

    def do_GET(self) -> None:
        type(self).seen.append({"path": self.path, "auth": self.headers["Authorization"]})
        if self.path.split("?")[0] != "/health" or self.health is None:
            self._send(404)
        else:
            self._send(200, self.health)

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
    answers: dict[int, dict] = {}  # a test's overrides of _GUARD_ANSWERS, by red value

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if "image_base64" in body:
            image = Image.open(io.BytesIO(base64.b64decode(body["image_base64"])))
            red = image.convert("RGB").getpixel((0, 0))[0]
            size, fmt = image.size, image.format
        else:  # a text-only check: the text picks the answer
            red, size, fmt = _TEXT_ANSWERS.get(body["input"], 10), None, None
        type(self).seen.append(
            {"path": self.path, "size": size, "format": fmt,
             **{k: v for k, v in body.items() if k != "image_base64"}}
        )  # fmt: skip
        if red == _FAIL:
            self._send(500)
        else:
            answer = self.answers.get(red) or _GUARD_ANSWERS.get(red, _GUARD_ANSWERS[10])
            self._send(200, {**answer, "model": "SingGuard-4b", "elapsed_ms": 5})


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


def _rated(**labels: float) -> NsfwResult:
    return NsfwResult(labels=labels, exposed_score=exposed_score(labels))


@pytest.mark.parametrize(
    ("labels", "rating"),
    [
        ({}, "safe"),
        ({"FACE_FEMALE": 0.9, "BELLY_EXPOSED": 0.8, "MALE_BREAST_EXPOSED": 0.7, "FEET_EXPOSED": 0.6}, "safe"),
        ({"BELLY_COVERED": 0.9, "ARMPITS_COVERED": 0.9, "FEET_COVERED": 0.9}, "safe"),  # covered, but not intimate
        ({"BUTTOCKS_COVERED": 0.3}, "suggestive"),
        ({"FEMALE_BREAST_COVERED": 0.25, "FACE_FEMALE": 0.9}, "suggestive"),
        ({"FEMALE_BREAST_EXPOSED": 0.21, "BUTTOCKS_COVERED": 0.9}, "explicit"),  # exposed wins over covered
        ({"MALE_GENITALIA_EXPOSED": 0.5}, "explicit"),
    ],
)
def test_content_rating_from_detections(labels: dict[str, float], rating: str) -> None:
    assert rate_content(_rated(**labels)) == rating


def test_content_rating_starts_at_nudenets_floor_and_an_unchecked_image_has_none() -> None:
    floor = nudenet.MIN_REPORTED_SCORE
    just_below = round(floor - 0.01, 2)
    assert rate_content(_rated(FEMALE_BREAST_EXPOSED=just_below)) == "safe"
    assert rate_content(_rated(FEMALE_BREAST_EXPOSED=floor)) == "explicit"
    assert rate_content(_rated(BUTTOCKS_COVERED=just_below)) == "safe"
    assert rate_content(_rated(BUTTOCKS_COVERED=floor)) == "suggestive"
    assert rate_content(None) is None  # not "safe": nobody looked
    assert rate_content(NsfwResult()) == "safe"


def test_content_ratings_are_ordered_and_the_two_label_sets_are_apart() -> None:
    assert CONTENT_RATINGS == ("safe", "suggestive", "explicit")
    assert set(SUGGESTIVE_LABELS) == {
        "FEMALE_BREAST_COVERED", "FEMALE_GENITALIA_COVERED", "BUTTOCKS_COVERED", "ANUS_COVERED",
    }  # fmt: skip
    assert not set(SUGGESTIVE_LABELS) & set(DEFAULT_EXPOSED_LABELS)
    assert set(SUGGESTIVE_LABELS) <= set(nudenet.NUDENET_LABELS)


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
_TEXT_ANSWERS = {"rob a bank": 20, "fail": _FAIL}  # a text-only check's input -> the red value it answers as


def test_singguard_config() -> None:
    client = SingGuardClient("http://192.0.2.198:9150/")
    assert client.url == "http://192.0.2.198:9150/api/guard/check"
    assert client.policy is None and client.policy_name == "default" and client.thinking == "fast"
    custom = SingGuardClient("http://g", policy="  ### A. Minors\n")
    assert custom.policy == "### A. Minors" and custom.policy_name == policy_name("### A. Minors")
    assert custom.policy_name == policy_name("  ### A. Minors\n")  # a policy read from a file matches
    assert custom.policy_name.startswith("custom-") and len(custom.policy_name) == len("custom-") + 12
    assert [SingGuardClient("http://g", thinking=m).thinking for m in ("slow", "cascade")] == ["slow", "cascade"]
    for kwargs in ({"thinking": "slower"}, {"thinking": "FAST"}, {"policy": " \n"}):
        with pytest.raises(ModerationConfigError):
            SingGuardClient("http://g", **kwargs)
    with pytest.raises(ModerationConfigError):
        SingGuardClient("gopher://g")


def test_to_result_keeps_a_long_explanation() -> None:
    writeup = "Cascade: fast flagged this; the slow pass is the verdict.\n\n" + ("A posed adult. " * 400)
    writeup = writeup.strip()
    assert 4000 < len(writeup) < singguard._EXPLANATION_MAX
    result = singguard.to_result(
        {"safe": True, "verdict": "safe", "rule": "Safe", "explanation": writeup, "categories": []}
    )
    assert result.explanation == writeup


def test_to_result_clips_explanation_at_max() -> None:
    exact = "y" * singguard._EXPLANATION_MAX
    over = exact + "z"
    payload = {"safe": True, "verdict": "safe", "rule": "Safe", "categories": []}
    assert singguard.to_result({**payload, "explanation": exact}).explanation == exact
    assert singguard.to_result({**payload, "explanation": over}).explanation == exact


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


def test_singguard_check_text(singguard_server) -> None:
    """A text-only check sends no image, says whose text it is, and keeps the client's policy and mode."""
    fake, url = singguard_server
    client = SingGuardClient(url, timeout=5, thinking="cascade", policy="### A. Minors")
    assert client.check_text("a cat on a sofa").safe
    assert client.check_text("rob a bank", role="assistant").rules == [_CRIME, _GORE]
    with pytest.raises(GuardCheckFailed, match="HTTP 500"):
        client.check_text("fail")
    assert all("image_base64" not in s and s["size"] is None for s in fake.seen)
    assert [s["input_role"] for s in fake.seen] == ["user", "assistant", "user"]
    assert {s["thinking"] for s in fake.seen} == {"cascade"} and {s["policy"] for s in fake.seen} == {"### A. Minors"}
    for text, role in (("", "user"), (" \n", "user"), ("hi", "system")):
        with pytest.raises(ValueError):
            client.check_text(text, role=role)
    assert len(fake.seen) == 3  # a bad argument never reaches the server


def test_singguard_default_policy_is_not_sent(singguard_server) -> None:
    fake, url = singguard_server
    SingGuardClient(url).check(_png(10))
    assert "policy" not in fake.seen[0]


def test_singguard_slow_is_sent_and_the_mode_used_is_recorded(singguard_server) -> None:
    """The argus-cull service answers with the mode it judged in, which a
    server-side force can make differ from the request; it is recorded as sent.
    A server that says nothing leaves it None."""
    fake, url = singguard_server
    client = SingGuardClient(url, timeout=5, thinking="slow")
    assert client.check(_png(10)).thinking is None
    fake.answers = {10: {**_GUARD_ANSWERS[10], "thinking": "fast-slow"}}
    try:
        assert client.check(_png(10)).thinking == "fast-slow"
    finally:
        fake.answers = {}
    assert {s["thinking"] for s in fake.seen} == {"slow"}


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


@pytest.mark.parametrize("size", [(64, 64), (4000, 3000)])
def test_downscale_refuses_multi_frame_images(size: tuple[int, int]) -> None:
    """A benign first frame must not stand in for the frames after it, small or large."""
    frames = [Image.new("RGB", size, c) for c in ("white", "red")]
    buf = io.BytesIO()
    frames[0].save(buf, "GIF", save_all=True, append_images=frames[1:])
    with pytest.raises(GuardCheckFailed, match="2 frames"):
        singguard.downscale(buf.getvalue())


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
    for name in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")  # nothing listens there
    for name in ("NO_PROXY", "no_proxy"):  # a CI shell's no_proxy=localhost would bypass the dead proxy anyway
        monkeypatch.delenv(name, raising=False)
    assert NudeNetClient(url, timeout=5).check(_png(10)) == NsfwResult()


def test_unreachable_server_is_a_failed_check() -> None:
    with pytest.raises(NudeNetCheckFailed, match="request failed"):
        NudeNetClient("http://127.0.0.1:9", timeout=2).check(_png(10))


def test_import_does_not_pull_in_pillow() -> None:
    code = "import sys, argus_cortex, argus_cortex.moderation; assert 'PIL' not in sys.modules"
    subprocess.run([sys.executable, "-c", code], check=True)


# ---------------------------------------------------------------------------
# NudeNet server info (#79)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("health", "model", "resolution", "detector"),
    [
        ({"ok": True, "model": "640m.onnx", "resolution": 640}, "640m.onnx", 640, "640m"),
        ({"ok": True, "model": "/models/320n.onnx"}, "/models/320n.onnx", None, "320n"),
        ({"ok": True, "model": "erax.pt", "resolution": "640"}, "erax.pt", None, None),
        ({"ok": True, "model": 7, "resolution": True}, None, None, None),
        ({"ok": True}, None, None, None),
        ([1, 2], None, None, None),
        (None, None, None, None),  # no /health route: the upstream 320n image
    ],
)
def test_nudenet_info(nudenet_server, monkeypatch: pytest.MonkeyPatch, health, model, resolution, detector) -> None:
    handler, url = nudenet_server
    monkeypatch.setattr(handler, "health", health)
    info = NudeNetClient(url).info()
    assert (info.model, info.resolution, info.detector) == (model, resolution, detector)
    assert handler.seen[-1]["path"] == "/health"


@pytest.mark.parametrize("suffix", ["", "/", "/infer", "/infer?k=v"])
def test_nudenet_health_url_sits_beside_infer(suffix: str) -> None:
    client = NudeNetClient(f"http://user:pw@nudenet:8080/sub{suffix}")
    query = "?k=v" if "?" in suffix else ""
    assert client.url == f"http://user:pw@nudenet:8080/sub/infer{query}"
    assert client.health_url == f"http://user:pw@nudenet:8080/sub/health{query}"


def test_nudenet_info_sends_credentials(nudenet_server, monkeypatch: pytest.MonkeyPatch) -> None:
    handler, url = nudenet_server
    monkeypatch.setattr(handler, "health", {"model": "640m.onnx"})
    NudeNetClient(url.replace("http://", "http://u:p@")).info()
    assert handler.seen[-1]["auth"] == "Basic dTpw"


def test_nudenet_info_unreachable_is_a_failed_check() -> None:
    with pytest.raises(NudeNetCheckFailed, match="request failed"):
        NudeNetClient("http://127.0.0.1:9", timeout=2).info()


def test_nudenet_info_server_error_is_a_failed_check(nudenet_server, monkeypatch: pytest.MonkeyPatch) -> None:
    handler, url = nudenet_server
    monkeypatch.setattr(handler, "do_GET", lambda self: self._send(500))
    with pytest.raises(NudeNetCheckFailed, match="HTTP 500"):
        NudeNetClient(url).info()


# ---------------------------------------------------------------------------
# OCR (argus-retina#5)
# ---------------------------------------------------------------------------

# Lines as argus-retina's OCR image answers them for the Curie Nobel diploma: words run together.
_OCR_LINES = {
    10: [],
    40: [
        {"class": "text", "score": 0.98, "box": [211, 285, 286, 32], "text": "KONGLIGA SVENSKA"},
        {"class": "text", "score": 0.93, "box": [180, 340, 330, 28], "text": "afProfessor"},
        {"class": "text", "score": 0.91, "box": [190, 380, 300, 30], "text": "enriBequerel"},
        {"class": "text", "score": 0.55, "box": [200, 420, 120, 20], "text": "N.Y.Public"},
        {"class": "text", "score": 0.52, "box": [330, 420, 90, 20], "text": "Library"},
    ],
}


class _Ocr(_Fake):
    health: dict | None = {
        "model_state": "cpu",
        "model": "PP-OCRv6 small (RapidOCR)",
        "det": "PP-OCRv6_det_small.onnx",
        "rec": "PP-OCRv6_rec_small.onnx",
        "min_score": 0.5,
    }

    def do_GET(self) -> None:
        type(self).seen.append({"path": self.path, "auth": self.headers["Authorization"]})
        if self.path.split("?")[0] != "/health" or self.health is None:
            self._send(404)
        else:
            self._send(200, self.health)

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers["Content-Length"]))
        msg = BytesParser(policy=HTTP).parsebytes(
            f"Content-Type: {self.headers['Content-Type']}\r\n\r\n".encode() + body
        )
        part = next(msg.iter_parts())
        red = _red(part.get_content())
        type(self).seen.append({"path": self.path, "field": part.get_param("name", header="content-disposition")})
        if red == _FAIL:
            self._send(500)
        else:
            self._send(200, {"prediction": [_OCR_LINES.get(red, [])], "success": True})


@pytest.fixture
def ocr_server() -> Iterator[tuple[type[_Fake], str]]:
    yield from _serve(_Ocr)


def test_ocr_read(ocr_server) -> None:
    fake, url = ocr_server
    client = OcrClient(url, timeout=5)
    assert client.url == f"{url}/infer" and client.health_url == f"{url}/health"
    assert client.read(_png(10)) == OcrResult()
    diploma = client.read(_png(40), "diploma.jpg")
    assert [line.text for line in diploma.lines][:3] == ["KONGLIGA SVENSKA", "afProfessor", "enriBequerel"]
    assert diploma.lines[0].score == 0.98 and diploma.lines[0].bbox == [211, 285, 286, 32]
    with pytest.raises(OcrCheckFailed, match="HTTP 500"):
        client.read(_png(_FAIL))
    assert {s["path"] for s in fake.seen} == {"/infer"} and {s["field"] for s in fake.seen} == {"f1"}


def test_ocr_transcript_keeps_reading_order_and_filters_by_score(ocr_server) -> None:
    _fake, url = ocr_server
    diploma = OcrClient(url, timeout=5).read(_png(40))
    assert diploma.transcript().splitlines() == [
        "KONGLIGA SVENSKA", "afProfessor", "enriBequerel", "N.Y.Public", "Library"
    ]  # fmt: skip
    assert diploma.transcript(min_score=0.9) == "KONGLIGA SVENSKA\nafProfessor\nenriBequerel"


def test_ocr_contains_compares_without_spaces_case_or_punctuation(ocr_server) -> None:
    _fake, url = ocr_server
    diploma = OcrClient(url, timeout=5).read(_png(40))
    assert diploma.contains("Professor")
    assert diploma.contains("af professor")  # run together on the page
    assert diploma.contains("Kongliga Svenska")
    assert diploma.contains("N.Y. Public Library")  # across two lines, punctuation and spaces aside
    assert not diploma.contains("Henrik Bohr")  # what a VLM wrote: not on the page
    assert not diploma.contains("Becquerel")  # the diploma spells it Bequerel; a check reads what is printed
    assert not diploma.contains("N.Y. Public Library", min_score=0.9)  # those lines were barely read
    assert not diploma.contains("") and not diploma.contains(" .,")  # nothing to have read
    assert not OcrResult().contains("anything")


def test_normalize_text() -> None:
    assert normalize_text("  N.Y. Public\tLibrary ") == "nypubliclibrary"
    assert normalize_text("STRASSE") == normalize_text("Straße")  # casefold, not lower
    assert normalize_text("ﬁle") == "file"  # NFKC: ligatures and full-width forms
    assert normalize_text("Espinàs") != normalize_text("Espinas")  # accents are the caller's policy


def test_normalize_text_keeps_combining_marks() -> None:
    """A mark with no precomposed letter is not alphanumeric on its own, and is still part of the word."""
    assert normalize_text("हिन्दी") == "हिन्दी"  # vowel signs and the virama stay
    assert normalize_text("किताब") != normalize_text("कताब")  # so words that differ only by a vowel sign differ
    assert normalize_text("ไก่") != normalize_text("ไก้")  # Thai tone marks
    assert normalize_text("q\u0303a") != normalize_text("qa")  # a Latin letter with no precomposed form
    assert normalize_text("Espin\u0061\u0300s") == normalize_text("Espinàs")  # NFKC still composes what it can
    assert normalize_text("हिन्दी, भाषा!") == "हिन्दीभाषा"  # spaces and punctuation still go


def test_is_number_refuses_an_int_past_the_float_range() -> None:
    from argus_cortex.moderation._http import is_number

    huge = json.loads("1" + "0" * 400)  # json.loads returns it exactly; math.isfinite alone raises OverflowError
    assert not is_number(huge) and not is_number(-huge)
    assert is_number(0) and is_number(10**300) and is_number(0.5)
    assert not is_number(True) and not is_number(float("nan")) and not is_number(float("inf")) and not is_number("1")


def test_ocr_parsing_ignores_malformed_entries() -> None:
    good = {"class": "text", "score": 0.8, "box": [1, 2, 3, 4], "text": "Norwegen."}
    r = ocr.to_result(
        [
            good,
            "not a dict",
            {**good, "class": "FACE_FEMALE"},  # not a text line
            {**good, "text": None},
            {**good, "text": "   "},
            {**good, "score": float("nan")},
            {**good, "score": True},
            {**good, "score": 1.5},
            {**good, "box": [1, 2, 3]},
            {**good, "box": [1, 2, 3, float("inf")]},
            {**good, "text": "Second", "score": 0.61234567},
        ]
    )
    assert [(line.text, line.score) for line in r.lines] == [("Norwegen.", 0.8), ("Second", 0.6123)]


@pytest.mark.parametrize(
    "payload",
    [
        {"prediction": [[]], "success": False},
        {"prediction": [], "success": True},
        {"prediction": [[], []], "success": True},
        {"prediction": ["x"], "success": True},
        {"success": True},
        [],
    ],
)
def test_ocr_bad_responses_are_failures(monkeypatch: pytest.MonkeyPatch, payload) -> None:
    monkeypatch.setattr(ocr, "post_json", lambda *a, **k: payload)
    with pytest.raises(OcrCheckFailed):
        OcrClient("http://ocr:8133").read(b"x")


def test_ocr_unreachable_server_is_a_failed_check() -> None:
    """A server that is down raises: a caller must never drop a claim because nothing was read."""
    with pytest.raises(OcrCheckFailed, match="request failed"):
        OcrClient("http://127.0.0.1:9", timeout=2).read(_png(10))
    with pytest.raises(OcrCheckFailed, match="request failed"):
        OcrClient("http://127.0.0.1:9", timeout=2).info()


@pytest.mark.parametrize(
    ("health", "expected"),
    [
        (
            _Ocr.health,
            OcrInfo(
                model="PP-OCRv6 small (RapidOCR)",
                det="PP-OCRv6_det_small.onnx",
                rec="PP-OCRv6_rec_small.onnx",
                min_score=0.5,
            ),
        ),
        ({"model": "", "det": 3, "min_score": "0.5"}, OcrInfo()),
        ({"min_score": True}, OcrInfo()),
        ([1], OcrInfo()),
        (None, OcrInfo()),  # no /health route
    ],
)
def test_ocr_info(ocr_server, monkeypatch: pytest.MonkeyPatch, health, expected) -> None:
    handler, url = ocr_server
    monkeypatch.setattr(handler, "health", health)
    assert OcrClient(url.replace("http://", "http://u:p@")).info() == expected
    assert handler.seen[-1] == {"path": "/health", "auth": "Basic dTpw"}


def test_ocr_url() -> None:
    assert OcrClient("http://ocr:8133/").url == "http://ocr:8133/infer"
    with pytest.raises(ModerationConfigError) as exc:
        OcrClient("ftp://user:secret@ocr")
    assert "secret" not in str(exc.value)
