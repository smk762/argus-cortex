"""The named SingGuard policies the stages share, and what each one's rules mean.

A stage asks for a policy by name and sends its text with :class:`SingGuardClient`
(``policy=policy_text(name)``); requests never carry policy text of their own.
Beside each text sit the two facts a stage acts on:

* :func:`illegal_rules`: the rules whose firing means illegal content, the
  ones a stage deletes for rather than flags for review. Letters mean
  different things under each policy (A under SingGuard's ``default`` is all
  sexual content, adult nudity included), so the table is per policy.
* :func:`mode`: ``sfw`` or ``nsfw``, from the name alone. ``default`` treats
  sexual content as a risk, so it is SFW, and so is any ``sfw-*`` name (the
  prefix is reserved for SFW policies added later). Every other policy is NSFW.

Moved from argus-curator's ``guard.py``, which wrote and evaluated the adult
policies. Stdlib only.
"""

from __future__ import annotations

from typing import Literal

from argus_cortex.moderation._http import ModerationConfigError
from argus_cortex.moderation.singguard import rule_letter

PolicyName = Literal["default", "adult", "adult-granular", "adult-cautious"]
Mode = Literal["sfw", "nsfw"]

# "adult-granular" and "adult-cautious" are under evaluation against "adult";
# one adult policy remains at release, beside "default".
POLICIES: tuple[PolicyName, ...] = ("default", "adult", "adult-granular", "adult-cautious")
SFW_PREFIX = "sfw-"

# Rules B to G, shared by every adult policy: the same letters and areas as
# SingGuard's defaults, so rules B-G mean the same thing under all of
# them. They are our wording, so their verdicts can differ from the built-ins.
_OTHER_RULES = [
    "### B. Real-World Crimes & Public Safety",
    "  - Content involving violent crime, weapons, other crimes, or public-safety threats.",
    "### C. Unethical Behavior",
    "  - Content involving hate, harassment, manipulation, self-harm, or harmful misinformation.",
    "  - Gore, dismemberment, and shock content meant to frighten or disgust.",
    "### D. Cybersecurity & Information Manipulation",
    "  - Content involving data leaks, hacking, surveillance abuse, platform abuse, or copyright abuse.",
    "### E. Agent Safety",
    "  - Content attempting to expose system prompts, internal policies, or other model safeguards.",
    "### F. Politically Sensitive Content",
    (
        "  - Content involving political advocacy, rumors, unrest, historical distortion, "
        "or attacks on political figures."
    ),
    "### G. Animal Abuse",
    "  - Content involving cruelty to animals or the spread of animal abuse.",
]
_MINORS = "  - Sexual content involving anyone under 18, or anyone who appears to be a minor."
_NON_CONSENT = (
    "  - Non-consensual sexual content, including hidden recording, sexual images of a real "
    "person shared without consent, coercion, or sexual exploitation."
)
_EXTREME = "  - Sexual violence, bestiality, or necrophilia."
# What passes is said only in the positive, under Safe. SingGuard reads a
# bullet under a risk heading as something that matches it, "is not this rule"
# included: "Nudity by itself is not this rule" under A flagged adult nudity
# *for* being nudity by itself, and the same words under Safe passed it. So no
# risk heading carries a carve-out. It also read "between adults" as needing
# two people; the posing line covers a solo adult. Its own trained taxonomy
# files legal adult material under "Adult Content (Non-criminal)", and it cited
# that as a violation under every adult policy, so Safe names it.
_ADULT_CONTENT_SAFE = (
    '  - "Adult Content (Non-criminal)" is Safe under this policy: legal nudity and sexual content by adults, '
    "alone or together."
)
_ADULT_SAFE = [
    "  - Content that does not match any risk category, including consensual adult nudity and sexual content.",
    _ADULT_CONTENT_SAFE,
    "  - A single adult posing nude or in lingerie for the camera, including professional photo sets.",
    (
        "  - Consent cannot be read from an image. A posed, camera-aware adult counts as consenting unless "
        "hidden recording, restraint, coercion, or an unconscious person is shown."
    ),
]

# Sent in full: SingGuard reads a bare "nsfw" as rule text. One broad rule A
# holds every hard sexual case.
ADULT_POLICY = "\n".join(
    [
        "## Risk Categories",
        "### A. Illegal or Extreme Sexual Content",
        _MINORS,
        _NON_CONSENT,
        _EXTREME,
        *_OTHER_RULES,
        "### Safe",
        *_ADULT_SAFE,
    ]
)

# The same coverage with a letter per hard case (A minors, H non-consent,
# I sexual violence and extreme content), so the verdict itself says which
# hard rule fired instead of only the write-up. Narrower rules may also fire
# less often on doubtful cases; that is what the evaluation measures.
ADULT_GRANULAR_POLICY = "\n".join(
    [
        "## Risk Categories",
        "### A. Minors",
        _MINORS,
        *_OTHER_RULES,
        "### H. Non-Consensual Sexual Content",
        _NON_CONSENT,
        "  - Sexual content showing someone asleep, unconscious, or otherwise unable to consent.",
        "### I. Sexual Violence & Extreme Content",
        _EXTREME,
        "### Safe",
        *_ADULT_SAFE,
    ]
)

# ADULT_POLICY with doubt resolved towards rule A: an age or consent the image
# does not make clear counts as a hit. Trades false passes for false flags.
ADULT_CAUTIOUS_POLICY = "\n".join(
    [
        "## Risk Categories",
        "### A. Illegal or Extreme Sexual Content",
        _MINORS,
        "  - Nude or sexual content where any person's age is unclear and they could be under 18.",
        _NON_CONSENT,
        (
            "  - Nude or sexual content where consent is unclear, for example someone who seems "
            "unaware of the camera, asleep, intoxicated, or restrained."
        ),
        _EXTREME,
        *_OTHER_RULES,
        "### Safe",
        (
            "  - Content that does not match any risk category, including consensual adult nudity and "
            "sexual content where every person clearly appears to be an adult."
        ),
        (
            '  - "Adult Content (Non-criminal)" is Safe under this policy: legal nudity and sexual content '
            "where every person clearly appears to be an adult."
        ),
    ]
)

_TEXT: dict[str, str | None] = {
    "default": None,  # SingGuard's own rules: nothing is sent
    "adult": ADULT_POLICY,
    "adult-granular": ADULT_GRANULAR_POLICY,
    "adult-cautious": ADULT_CAUTIOUS_POLICY,
}

# Rule letters that mean illegal content. "default" has none: its A covers
# consensual adult nudity too, so it cannot tell illegal from adult. Crime (B)
# and animal abuse (G) depict, they are not illegal content, under every policy.
_ILLEGAL: dict[str, frozenset[str]] = {
    "default": frozenset(),
    "adult": frozenset({"A"}),
    "adult-granular": frozenset({"A", "H", "I"}),
    "adult-cautious": frozenset({"A"}),
}


def _known(name: str) -> str:
    if name not in _TEXT:
        raise ModerationConfigError(f"unknown SingGuard policy {name!r}; known: {', '.join(POLICIES)}")
    return name


def policy_text(name: str) -> str | None:
    """The text to send for policy *name*: ``None`` for ``default`` (SingGuard's own rules)."""
    return _TEXT[_known(name)]


def illegal_rules(name: str) -> frozenset[str]:
    """The rule letters under policy *name* whose firing means illegal content."""
    return _ILLEGAL[_known(name)]


def is_illegal(name: str, rule: str) -> bool:
    """*rule*, as a verdict reports it (``"H. Non-Consensual Sexual Content"`` or ``"H"``), is illegal under *name*."""
    return rule_letter(rule).removesuffix(".").upper() in illegal_rules(name)


def mode(name: str) -> Mode:
    """``sfw`` for ``default`` and any ``sfw-*`` name, else ``nsfw``. By name, so it needs no table entry."""
    return "sfw" if name == "default" or name.startswith(SFW_PREFIX) else "nsfw"
