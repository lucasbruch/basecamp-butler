"""Link-header pagination parsing."""
from app.basecamp.client import _next_link


def test_finds_next_url():
    header = '<https://3.basecampapi.com/1/projects.json?page=2>; rel="next"'
    assert _next_link(header) == "https://3.basecampapi.com/1/projects.json?page=2"


def test_picks_next_among_several_rels():
    header = (
        '<https://x/prev>; rel="prev", '
        '<https://x/next>; rel="next", '
        '<https://x/last>; rel="last"'
    )
    assert _next_link(header) == "https://x/next"


def test_no_next_returns_none():
    assert _next_link('<https://x/last>; rel="last"') is None
    assert _next_link("") is None
