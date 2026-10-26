"""SingGuard client: a policy verdict from a SingGuard server over HTTP.

Where NudeNet scores nudity per body part, SingGuard judges an image (and
optionally text) against a whole moderation policy and answers with a verdict
and the rule that fired. Its default rules are A. Sexual Content Risk, B.
Real-World Crimes & Public Safety, C. Unethical Behavior (hate, self-harm,
gore), D. Cybersecurity, E. Agent Safety, F. Politically Sensitive and G.
Animal Abuse. A custom policy text replaces them. It gives no scores.

API: ``POST /api/guard/check`` with JSON ``{input, image_base64, policy,
thinking}``, answered by ``{safe, verdict, rule, explanation, categories:
[{code, label, why}], model, elapsed_ms}``.

Large images are downscaled before sending (Pillow, from the ``[images]``
extra, imported only when downscaling is on).
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import io
import json

from pydantic import BaseModel, Field

from argus_cortex.moderation._http import CheckFailed, ModerationConfigError, post_json, service_url

THINKING_MODES = ("fast", "fast-slow")
# The guard model sees far fewer pixels than a camera original, and base64
# JSON inflates every byte by 4/3.
MAX_EDGE = 1536
MAX_BYTES = 4 * 1024 * 1024
_MIN_EDGE = 256  # below this, give up rather than send SingGuard a postage stamp
_NEEDS_PILLOW = "downscaling images needs: pip install argus-cortex[images] (or pass downscale=False)"
_EXPLANATION_MAX = 4000
_FIELD_MAX = 200


class GuardResult(BaseModel):
    """SingGuard's verdict on one image. A verdict, not a score: SingGuard
    reports which policy rules fired, not how confident it is."""

    safe: bool
    # The rules that made the image unsafe, as the policy names them
    # (e.g. "A. Sexual Content Risk"); empty when safe.
    rules: list[str] = Field(default_factory=list)
    explanation: str | None = None
    # The guard model, and the policy judged against: "default" (SingGuard's
    # own rules) or "custom-<sha256 prefix>" of the policy text.
    model: str | None = None
    policy: str = "default"


class GuardCheckFailed(CheckFailed):
    """One image could not be checked by SingGuard."""


def policy_name(text: str | None) -> str:
    """How a result records the policy: ``default``, or ``custom-`` + a sha256 prefix of *text*.

    *text* is stripped first, as :class:`SingGuardClient` strips the policy it sends.
    """
    return "default" if text is None else f"custom-{hashlib.sha256(text.strip().encode()).hexdigest()[:12]}"


def rule_letter(rule: str) -> str:
    """*rule* by its letter (``"A"`` for ``"A. Sexual Content Risk"``), else in full."""
    head, sep, _ = rule.strip().partition(".")
    return head if sep and len(head) == 1 else rule


def rule_matches(fired: str, wanted: str) -> bool:
    """*wanted* names *fired*: in full, or by its letter (``"A"`` or ``"A."`` for ``"A. Sexual Content Risk"``)."""
    fired, wanted = fired.strip().casefold(), wanted.strip().casefold()
    return fired == wanted or fired.startswith(f"{wanted.removesuffix('.')}.")


def _clip(value: object, limit: int) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()[:limit]


def to_result(payload: object, policy: str = "default") -> GuardResult:
    """SingGuard's answer as recorded. A missing verdict is a failure, not a pass."""
    if not isinstance(payload, dict) or not isinstance(payload.get("safe"), bool):
        raise GuardCheckFailed("SingGuard returned no verdict")
    safe = payload["safe"]
    rules: list[str] = []
    if not safe:
        categories = payload.get("categories")
        for cat in categories if isinstance(categories, list) else []:
            code = _clip(cat.get("code"), _FIELD_MAX) if isinstance(cat, dict) else None
            if code and code not in rules:
                rules.append(code)
        rule = _clip(payload.get("rule"), _FIELD_MAX)
        if not rules and rule:
            rules.append(rule)
    return GuardResult(
        safe=safe,
        rules=rules,
        explanation=_clip(payload.get("explanation"), _EXPLANATION_MAX),
        model=_clip(payload.get("model"), _FIELD_MAX),
        policy=policy,
    )


def downscale(data: bytes, max_edge: int = MAX_EDGE, max_bytes: int = MAX_BYTES) -> bytes:
    """*data* as is when small enough, else downscaled to a JPEG (a PNG when it
    has alpha) of at most *max_edge* px and *max_bytes*, shrinking further if
    the first encoding is still too large.

    Raises :class:`GuardCheckFailed` for an image Pillow cannot read or shrink
    under *max_bytes*.
    """
    try:
        from PIL import Image, ImageOps
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise ModerationConfigError(_NEEDS_PILLOW) from exc
    try:
        with Image.open(io.BytesIO(data)) as im:
            if max(im.size) <= max_edge and len(data) <= max_bytes:
                return data
            # Shrink first, so a JPEG decodes at reduced scale (draft mode);
            # the box is square, so transposing after gives the same size.
            im.thumbnail((max_edge, max_edge))
            # Upright, as a viewer would see it: re-encoding drops the EXIF tag.
            out = ImageOps.exif_transpose(im)
            alpha = out.mode in ("RGBA", "LA", "PA") or (out.mode == "P" and "transparency" in out.info)
            if not alpha:
                out = out.convert("RGB")
            edge = max(out.size)
            while True:
                buf = io.BytesIO()
                if alpha:
                    # Flattening onto black would hide dark content drawn on a
                    # transparent background; keep the alpha, as a small PNG goes.
                    out.save(buf, "PNG", optimize=True)
                else:
                    out.save(buf, "JPEG", quality=90)
                if buf.tell() <= max_bytes:
                    return buf.getvalue()
                if edge <= _MIN_EDGE:
                    raise GuardCheckFailed("image could not be shrunk under the SingGuard size limit")
                edge = max(_MIN_EDGE, edge * 3 // 4)
                out.thumbnail((edge, edge))
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        raise GuardCheckFailed(f"image could not be prepared for SingGuard: {exc}") from exc


class SingGuardClient:
    """A SingGuard server at *base_url* (its origin, e.g. ``http://guard:9150``).

    *policy* is the policy text (``None`` = SingGuard's default rules);
    *thinking* is ``fast`` (verdict and rule) or ``fast-slow`` (also writes why).
    With *downscale* on (the default, needs ``[images]``), images over
    :data:`MAX_EDGE` px or :data:`MAX_BYTES` are shrunk before sending.
    Thread-safe: :meth:`check` holds no state.
    """

    def __init__(
        self,
        base_url: str,
        timeout: float = 90.0,
        thinking: str = "fast",
        policy: str | None = None,
        downscale: bool = True,
    ) -> None:
        if thinking not in THINKING_MODES:
            raise ModerationConfigError(f"SingGuard thinking must be one of {', '.join(THINKING_MODES)}")
        if policy is not None and not policy.strip():
            raise ModerationConfigError("the SingGuard policy is empty (pass None for the default rules)")
        if downscale and importlib.util.find_spec("PIL") is None:
            # Fail at startup, not as a config error out of every check().
            raise ModerationConfigError(_NEEDS_PILLOW)
        self.url = service_url(base_url, "/api/guard/check", "SingGuard")
        self.timeout = timeout
        self.thinking = thinking
        self.policy = policy.strip() if policy is not None else None
        self.policy_name = policy_name(self.policy)
        self.downscale = downscale

    def check(self, data: bytes, text: str = "") -> GuardResult:
        """Judge one image, with *text* (e.g. the prompt that made it) judged alongside.

        Raises :class:`GuardCheckFailed`.
        """
        image = downscale(data) if self.downscale else data
        body: dict[str, str] = {
            "input": text,
            "image_base64": base64.b64encode(image).decode("ascii"),
            "thinking": self.thinking,
        }
        if self.policy is not None:
            body["policy"] = self.policy
        payload = post_json(
            self.url, json.dumps(body).encode(), "application/json", self.timeout, "SingGuard", GuardCheckFailed
        )
        return to_result(payload, self.policy_name)
