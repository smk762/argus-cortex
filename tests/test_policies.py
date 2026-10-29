"""The named SingGuard policies: their texts, illegal rules and modes."""

from __future__ import annotations

import pytest

from argus_cortex.moderation import ModerationConfigError, SingGuardClient, policies, policy_name


def test_every_policy_has_a_text_and_an_illegal_table() -> None:
    assert policies.POLICIES == ("default", "adult", "adult-granular", "adult-cautious")
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
    # Each illegal letter is a heading in that policy's own text.
    for name in policies.POLICIES[1:]:
        for letter in policies.illegal_rules(name):
            assert f"\n### {letter}. " in policies.policy_text(name)


def test_is_illegal_reads_a_rule_as_verdicts_report_it() -> None:
    assert policies.is_illegal("adult-granular", "H. Non-Consensual Sexual Content")
    assert policies.is_illegal("adult-granular", "I") and policies.is_illegal("adult-granular", "a.")
    assert policies.is_illegal("adult", "A. Illegal or Extreme Sexual Content")
    assert not policies.is_illegal("default", "A. Sexual Content Risk")
    for name in policies.POLICIES:  # crime and animal abuse go to review, never deletion
        assert not policies.is_illegal(name, "B. Real-World Crimes & Public Safety")
        assert not policies.is_illegal(name, "G. Animal Abuse")
        assert not policies.is_illegal(name, "Safe")


def test_mode_is_by_name() -> None:
    assert policies.mode("default") == "sfw"
    assert policies.mode("sfw-strict") == "sfw"  # a later SFW policy needs no table entry
    assert [policies.mode(n) for n in policies.POLICIES[1:]] == ["nsfw"] * 3
    assert policies.mode("defaults") == "nsfw"


def test_unknown_policy_names_are_refused() -> None:
    for call in (policies.policy_text, policies.illegal_rules):
        with pytest.raises(ModerationConfigError, match="unknown SingGuard policy"):
            call("adult-lax")
    with pytest.raises(ModerationConfigError):
        policies.is_illegal("adult-lax", "A")
