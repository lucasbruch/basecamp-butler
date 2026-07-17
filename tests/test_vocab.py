"""The rule classifier's vocabulary matching. Regression guard for the switch
from substring matching to word-boundary matching."""
from app.classifier.vocab import (
    ACTION_SIGNALS,
    DOMAIN_TERMS,
    contains_any,
    matched_terms,
    mentions_name,
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


def test_mentions_unambiguous_first_name():
    assert mentions_name("hey Sam can you help", "Sam Lee")
    assert mentions_name("ask ANNA about it", "Anna")
    assert not mentions_name("nothing addressed here", "Sam Lee")


def test_mentions_full_name_always_counts():
    # Even an ambiguous first name is fine when the full name appears.
    assert mentions_name("please loop in Mark Reed on this", "Mark Reed")


def test_ambiguous_first_name_needs_vocative_cue():
    # "mark"/"will" are common words → a bare occurrence must NOT count.
    assert not mentions_name("mark the file as done", "Mark Twain")
    assert not mentions_name("will you send it over", "Will Byers")
    assert not mentions_name("the art department signed off", "Art Vandelay")
    # But a real address still registers.
    assert mentions_name("hey Mark, can you review", "Mark Twain")
    assert mentions_name("Will: please take a look", "Will Byers")
    assert mentions_name("cc Art on the thread", "Art Vandelay")


def test_mentions_empty_inputs():
    assert not mentions_name("some text", "")
    assert not mentions_name("", "Sam")
