"""Pings arrive one raw event per chat line, but a single ask often spans several
lines. The classifier groups a thread's new lines and reads them as one
conversation — these cover that grouping/transcript logic (pure, no DB)."""
from types import SimpleNamespace

from app.classifier import conversation


def _ev(event_id, chat_id, name, text, creator_id=None):
    return SimpleNamespace(
        id=event_id,
        type="ping",
        project_id=None,
        payload={
            "_chat_id": chat_id,
            "creator": {"id": creator_id, "name": name},
            "content": text,
        },
    )


def test_group_by_thread_buckets_and_preserves_order():
    events = [
        _ev(1, 100, "Anna", "hey"),
        _ev(2, 200, "Ben", "unrelated"),
        _ev(3, 100, "Anna", "can you look at the storyboard"),
        _ev(4, 100, "Anna", "need it by Friday"),
    ]
    groups = dict(conversation.group_by_thread(events))
    assert set(groups) == {100, 200}
    assert [e.id for e in groups[100]] == [1, 3, 4]  # chronological within thread
    assert [e.id for e in groups[200]] == [2]


def test_combined_and_latest_text():
    group = [
        _ev(1, 100, "Anna", "hey"),
        _ev(2, 100, "Anna", "can you review the <b>storyboard</b>"),  # tags stripped
        _ev(3, 100, "Anna", "by Friday"),
    ]
    combined = conversation.combined_text(group)
    assert "<b>" not in combined
    # All three lines are folded into one string the keyword rules can match.
    assert combined.split() == ["hey", "can", "you", "review", "the", "storyboard", "by", "Friday"]
    assert conversation.latest_text(group) == "by Friday"


def test_transcript_labels_owner_and_marks_new_messages():
    my_id = 42
    context = [_ev(1, 100, "You", "sure, go ahead", creator_id=42)]
    new = [
        _ev(2, 100, "Anna", "can you send the deck"),
        _ev(3, 100, "You", "", creator_id=42),  # empty body → dropped
    ]
    transcript = conversation.render_transcript(new, my_id, context)
    lines = transcript.splitlines()
    assert lines[0] == "You (you): sure, go ahead"   # own line tagged
    assert "--- new messages ---" in lines
    assert lines[-1] == "Anna: can you send the deck"  # empty own line dropped


def test_transcript_no_marker_without_context():
    new = [_ev(2, 100, "Anna", "ping me back")]
    transcript = conversation.render_transcript(new, my_id=1, context_events=[])
    assert transcript == "Anna: ping me back"
    assert "new messages" not in transcript
