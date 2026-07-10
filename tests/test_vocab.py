"""The rule classifier's vocabulary matching. Regression guard for the switch
from substring matching to word-boundary matching."""
from app.classifier.vocab import (
    ACTION_SIGNALS,
    DOMAIN_TERMS,
    contains_any,
    matched_terms,
)


def test_matches_whole_words():
    assert contains_any("please re-render the comp", ACTION_SIGNALS)
    assert contains_any("please re-render the comp", DOMAIN_TERMS)
    assert "comp" in matched_terms("final comp is due", DOMAIN_TERMS)
    assert "render" in matched_terms("kick off the render", DOMAIN_TERMS)


def test_no_substring_false_positives():
    # These previously tripped short tokens: led/comp/spec/round/sim/rig.
    assert not contains_any("i called you about the company", DOMAIN_TERMS)
    assert not contains_any("scheduled for especially around noon", DOMAIN_TERMS)
    assert not contains_any("this is a simple bright idea", DOMAIN_TERMS)


def test_multiword_and_separator_variants():
    # "sign off" / "sign-off" / "sign_off" should all match the one term.
    for variant in ("please sign off", "please sign-off", "please sign_off"):
        assert contains_any(variant, ACTION_SIGNALS), variant


def test_case_insensitive():
    assert contains_any("PLEASE DELIVER THE MASTER", ACTION_SIGNALS)
    assert contains_any("PLEASE DELIVER THE MASTER", DOMAIN_TERMS)
