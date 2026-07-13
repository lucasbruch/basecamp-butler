"""Pings arrive one raw event per chat line, but a single ask often spans several
lines. The classifier groups a thread's new lines and reads them as one
conversation — these cover that grouping/transcript logic (pure, no DB)."""
from types import SimpleNamespace

from app.classifier import conversation
from app.classifier.rules import _chat_verdict, _ping_verdict


def _ev(event_id, chat_id, name, text, creator_id=None, event_type="ping"):
    return SimpleNamespace(
        id=event_id,
        type=event_type,
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


def test_grouping_is_type_agnostic_for_campfire():
    # Campfire lines carry the same _chat_id key, so grouping works unchanged.
    events = [
        _ev(1, 500, "Cara", "morning all", event_type="chat"),
        _ev(2, 501, "Dan", "other room", event_type="chat"),
        _ev(3, 500, "Cara", "who owns the deploy", event_type="chat"),
    ]
    groups = dict(conversation.group_by_thread(events))
    assert [e.id for e in groups[500]] == [1, 3]
    assert [e.id for e in groups[501]] == [2]


def test_ping_verdict_gates_on_either_signal():
    # Pings are aimed at you → an action word OR a domain term is enough.
    assert _ping_verdict("can you take a look", "look", " from Anna", None, "Sam")
    # Pure chatter with neither signal → no to-do.
    assert _ping_verdict("haha nice one", "nice", "", None, "Sam") is None


def test_chat_verdict_needs_mention_or_action_plus_domain():
    # Your name in the room → flagged as a mention.
    hit = _chat_verdict("hey Sam can you help", "hey Sam...", "", None, "Sam Lee")
    assert hit and hit[1] == "mention:by-name"
    # No name and only a lone action word (no work noun) → not enough for chat.
    assert _chat_verdict("can you take a look", "look", "", None, "Sam") is None
