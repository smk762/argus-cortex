"""The named SingGuard policies: their texts, illegal rules and modes."""

from __future__ import annotations

import pytest

from argus_cortex.moderation import ModerationConfigError, SingGuardClient, policies, policy_name


def test_every_policy_has_a_text_and_an_illegal_table() -> None:
    assert policies.POLICIES == (
        "default",
        "adult",
        "adult-granular",
        "adult-cautious",
        "adult-cinematic",
    )
    assert policies.policy_text("default") is None  # SingGuard's own rules: nothing is sent
    for name in policies.POLICIES[1:]:
        text = policies.policy_text(name)
        assert text.startswith("## Risk Categories\n") and "### Safe" in text
        # The client sends it as written, so a recorded policy name is stable.
        assert SingGuardClient("http://g", policy=text, downscale=False).policy_name == policy_name(text)
    assert len({policies.policy_text(n) for n in policies.POLICIES}) == len(policies.POLICIES)


def test_illegal_rules_are_per_policy() -> None:
    assert policies.illegal_rules("default") == frozenset()  # its A is all sexual content, adult included
    assert policies.illegal_rules("adult-granular") == {"A", "H", "I"}
    assert policies.illegal_rules("adult") == policies.illegal_rules("adult-cautious") == {"A"}
    assert policies.illegal_rules("adult-cinematic") == {"A"}
    # Each illegal letter is a heading in that policy's own text.
    for name in policies.POLICIES[1:]:
        for letter in policies.illegal_rules(name):
            assert f"\n### {letter}. " in policies.policy_text(name)


def test_is_illegal_reads_a_rule_as_verdicts_report_it() -> None:
    assert policies.is_illegal("adult-granular", "H. Non-Consensual Sexual Content")
    assert policies.is_illegal("adult-granular", "I") and policies.is_illegal("adult-granular", "a.")
    assert policies.is_illegal("adult", "A. Illegal Sexual Content")
    assert policies.is_illegal("adult", "A. Illegal or Extreme Sexual Content")  # an older recorded title
    assert not policies.is_illegal("default", "A. Sexual Content Risk")
    for name in policies.POLICIES:  # crime and animal abuse go to review, never deletion
        assert not policies.is_illegal(name, "B. Real-World Crimes & Public Safety")
        assert not policies.is_illegal(name, "G. Animal Abuse")
        assert not policies.is_illegal(name, "Safe")


def test_adult_safe_names_explicit_commercial_content() -> None:
    for name in ("adult", "adult-granular", "adult-cautious", "adult-cinematic"):
        text = policies.policy_text(name)
        risks, safe = text.split("### Safe")
        assert "Extreme Content" not in risks
        assert "legal adult entertainment" in safe
        assert "commercial porn" in safe
        assert "adult-site watermark" in safe
        assert "closest heading" in safe
        assert "is not this rule" not in risks
        assert "between adults" not in text


def test_adult_cinematic_narrows_crime_and_keeps_adult_entertainment_safe() -> None:
    text = policies.policy_text("adult-cinematic")
    risks, safe = text.split("### Safe")
    assert "how to commit one" in risks
    assert "violent crime, weapons, other crimes" not in risks
    assert "bank robbery" in safe and "stealing pensions" in safe and "dirty cops" in safe
    assert "Legal adult entertainment is Safe under this policy, the same as under adult" in safe
    assert "between adults" not in text
    assert policies.policy_text("adult-cinematic") != policies.policy_text("adult")


def test_mode_is_by_name() -> None:
    assert policies.mode("default") == "sfw"
    assert policies.mode("sfw-strict") == "sfw"  # a later SFW policy needs no table entry
    assert [policies.mode(n) for n in policies.POLICIES[1:]] == ["nsfw"] * 4
    assert policies.mode("defaults") == "nsfw"


def test_unknown_policy_names_are_refused() -> None:
    for call in (policies.policy_text, policies.illegal_rules):
        with pytest.raises(ModerationConfigError, match="unknown SingGuard policy"):
            call("adult-lax")
    with pytest.raises(ModerationConfigError):
        policies.is_illegal("adult-lax", "A")
