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

PolicyName = Literal["default", "adult", "adult-granular", "adult-cautious", "adult-cinematic"]
Mode = Literal["sfw", "nsfw"]

# "adult-granular", "adult-cautious" and "adult-cinematic" are under evaluation
# against "adult"; one adult policy remains at release, beside "default".
POLICIES: tuple[PolicyName, ...] = (
    "default",
    "adult",
    "adult-granular",
    "adult-cautious",
    "adult-cinematic",
)
SFW_PREFIX = "sfw-"

# Rules B to G, shared by every adult policy except cinematic: the same
# letters and areas as SingGuard's defaults. They are our wording, so their
# verdicts can differ from the built-ins. Cinematic keeps C to F as they are
# and swaps what sits at B and G.
_CRIME_B = [
    "### B. Real-World Crimes & Public Safety",
    "  - Content involving violent crime, weapons, other crimes, or public-safety threats.",
]
# Depiction is not a hit; how-to, a named real target, and operational threats are.
# At G, not B: a heading called crimes at B was read as any crime shown, whatever
# its bullet said.
_CRIME_CINEMATIC = [
    "### G. Criminal Instructions",
    (
        "  - Instructional or operational assistance for a real-world crime: how to commit one, "
        "targeting a named real person or a real institution, or an operational public-safety threat."
    ),
]
_C_TO_F = [
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
]
_ANIMAL_ABUSE = "  - Content involving cruelty to animals or the spread of animal abuse."
_OTHER_RULES = [*_CRIME_B, *_C_TO_F, "### G. Animal Abuse", _ANIMAL_ABUSE]

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
# Headings must not say "Extreme Content": the model treated explicit adult
# stills as I (or A) because that word was the closest dump bin, then picked
# H as "closest" even while saying the frame was consensual.
_ADULT_CONTENT_SAFE = (
    '  - "Adult Content (Non-criminal)" is Safe under this policy: legal adult entertainment, '
    "legal nudity and sexual content by adults, alone or together, including commercial porn, "
    "explicit sex, genitalia, a suggestive pose, a commercial or studio set, and an adult-site watermark."
)
_ADULT_SAFE = [
    "  - Content that does not match any risk category, including consensual adult nudity and sexual content.",
    _ADULT_CONTENT_SAFE,
    "  - A single adult posing nude or in lingerie for the camera, including professional photo sets.",
    (
        "  - Consent cannot be read from an image. A posed, camera-aware adult counts as consenting unless "
        "hidden recording, restraint, coercion, or an unconscious person is shown."
    ),
    (
        "  - If no risk-category bullet matches, the verdict is Safe. Do not assign a risk letter "
        "because it is the closest heading."
    ),
]

# Sent in full: SingGuard reads a bare "nsfw" as rule text. One broad rule A
# holds every hard sexual case.
ADULT_POLICY = "\n".join(
    [
        "## Risk Categories",
        "### A. Illegal Sexual Content",
        _MINORS,
        _NON_CONSENT,
        _EXTREME,
        *_OTHER_RULES,
        "### Safe",
        *_ADULT_SAFE,
    ]
)

# The same coverage with a letter per hard case (A minors, H non-consent,
# I sexual violence), so the verdict itself says which hard rule fired
# instead of only the write-up. Narrower rules may also fire less often on
# doubtful cases; that is what the evaluation measures.
ADULT_GRANULAR_POLICY = "\n".join(
    [
        "## Risk Categories",
        "### A. Minors",
        _MINORS,
        *_OTHER_RULES,
        "### H. Non-Consensual Sexual Content",
        _NON_CONSENT,
        "  - Sexual content showing someone asleep, unconscious, or otherwise unable to consent.",
        "### I. Sexual Violence",
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
        "### A. Illegal Sexual Content",
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
            '  - "Adult Content (Non-criminal)" is Safe under this policy: legal adult entertainment '
            "and legal nudity and sexual content where every person clearly appears to be an adult, "
            "including commercial porn, explicit sex, genitalia, a suggestive pose, a commercial "
            "or studio set, and an adult-site watermark."
        ),
        (
            "  - If no risk-category bullet matches, the verdict is Safe. Do not assign a risk letter "
            "because it is the closest heading."
        ),
    ]
)

# ADULT_POLICY with no crimes heading at B: animal abuse moves to B, and G is
# how-to / real targeting only. C to F keep their letters and titles, so a letter
# filter means the same there as under the other policies, and a title the model
# was trained on stays a title of this policy. Depicted crime is named under
# Safe, and adult entertainment stays Safe the same way as under adult.
# The depiction bullet yields to every risk bullet and leaves sexual content to
# the adult bullets, so it cannot pass a rule or take Safe from a legal frame.
# Letters must stay contiguous: with a gap at B the model renumbered the rules
# in its reasoning, so verdicts named the wrong rule and some changed. Compared
# against adult; do not fold this into granular (that mix of lettering and
# crime permissiveness is issue #24).
ADULT_CINEMATIC_POLICY = "\n".join(
    [
        "## Risk Categories",
        "### A. Illegal Sexual Content",
        _MINORS,
        _NON_CONSENT,
        _EXTREME,
        "### B. Animal Abuse",
        _ANIMAL_ABUSE,
        *_C_TO_F,
        *_CRIME_CINEMATIC,
        "### Safe",
        *_ADULT_SAFE,
        (
            "  - Legal adult entertainment is Safe under this policy, the same as under adult: "
            "commercial porn, studio stills, explicit sex, and an adult-site watermark."
        ),
        (
            "  - If no risk-category bullet matches, a non-sexual depiction of crime or violence is Safe, as in "
            "film, television, games, and print, and it needs no sign that it is fiction: a robbery, a heist, "
            "a kidnapping, a person bound or held captive, theft, fraud, corrupt officials, an armed standoff, "
            "a police raid, a fight, a battle, weapons, drugs, and war."
        ),
    ]
)

_TEXT: dict[str, str | None] = {
    "default": None,  # SingGuard's own rules: nothing is sent
    "adult": ADULT_POLICY,
    "adult-granular": ADULT_GRANULAR_POLICY,
    "adult-cautious": ADULT_CAUTIOUS_POLICY,
    "adult-cinematic": ADULT_CINEMATIC_POLICY,
}

# Rule letters that mean illegal content. "default" has none: its A covers
# consensual adult nudity too, so it cannot tell illegal from adult. Crime and
# animal abuse depict, they are not illegal content, under every policy, whatever
# their letters: B and G, or G and B under adult-cinematic.
_ILLEGAL: dict[str, frozenset[str]] = {
    "default": frozenset(),
    "adult": frozenset({"A"}),
    "adult-granular": frozenset({"A", "H", "I"}),
    "adult-cautious": frozenset({"A"}),
    "adult-cinematic": frozenset({"A"}),
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
