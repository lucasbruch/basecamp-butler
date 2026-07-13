"""The rule classifier's vocabulary matching. Regression guard for the switch
from substring matching to word-boundary matching."""
from app.classifier.vocab import (
    ACTION_SIGNALS,
    DOMAIN_TERMS,
    contains_any,
    matched_terms,
)


def test_matches_whole_words():
    assert contains_any("please send the report", ACTION_SIGNALS)
    assert contains_any("please send the report", DOMAIN_TERMS)
    assert "report" in matched_terms("final report is due", DOMAIN_TERMS)
    assert "invoice" in matched_terms("approve the invoice", DOMAIN_TERMS)


def test_no_substring_false_positives():
    # Word-boundary matching, so short tokens like "doc"/"app" don't trip on
    # longer words that merely contain them.
    assert not contains_any("i applied to the company", DOMAIN_TERMS)
    assert not contains_any("the documentation is on the intranet", DOMAIN_TERMS)
    assert not contains_any("this is a simple bright idea", DOMAIN_TERMS)


def test_multiword_and_separator_variants():
    # "sign off" / "sign-off" / "sign_off" should all match the one term.
    for variant in ("please sign off", "please sign-off", "please sign_off"):
        assert contains_any(variant, ACTION_SIGNALS), variant
    # And a multi-word domain term matches across the same separators.
    for variant in ("open a pull request", "open a pull-request"):
        assert contains_any(variant, DOMAIN_TERMS), variant


def test_case_insensitive():
    assert contains_any("PLEASE SEND THE INVOICE", ACTION_SIGNALS)
    assert contains_any("PLEASE SEND THE INVOICE", DOMAIN_TERMS)
