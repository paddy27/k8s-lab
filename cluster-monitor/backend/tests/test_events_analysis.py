from datetime import datetime, timezone

from app.events_analysis import filter_events, serialize_event, summarize_events

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _event(reason, event_type="Warning", namespace="obs", kind="Pod", name="backend-1", message="something happened"):
    return {
        "type": event_type,
        "reason": reason,
        "message": message,
        "involved_object": {"namespace": namespace, "kind": kind, "name": name},
        "count": 1,
        "last_timestamp": T0,
        "first_timestamp": T0,
    }


def test_summarize_events_counts_by_severity():
    events = [
        _event("FailedScheduling"),  # critical (in CRITICAL_EVENT_REASONS)
        _event("BackOff"),           # warning (Warning type, not in the critical set)
        _event("Started", event_type="Normal"),
        _event("Started", event_type="Normal"),
    ]

    summary = summarize_events(events)

    assert summary["critical"] == 1
    assert summary["warning"] == 1
    assert summary["normal"] == 2


def test_summarize_events_top_reasons_sorted_by_count():
    events = [_event("BackOff") for _ in range(3)] + [_event("FailedScheduling") for _ in range(5)]

    summary = summarize_events(events)

    assert summary["top_reasons"][0] == {"reason": "FailedScheduling", "count": 5}
    assert summary["top_reasons"][1] == {"reason": "BackOff", "count": 3}


def test_summarize_events_top_reasons_capped_at_five():
    events = [_event(f"Reason{i}") for i in range(8)]

    summary = summarize_events(events)

    assert len(summary["top_reasons"]) == 5


def test_serialize_event_shape():
    event = _event("BackOff", namespace="obs", kind="Pod", name="backend-1", message="back-off restarting")

    serialized = serialize_event(event)

    assert serialized["severity"] == "warning"
    assert serialized["namespace"] == "obs"
    assert serialized["involved_kind"] == "Pod"
    assert serialized["involved_name"] == "backend-1"
    assert serialized["message"] == "back-off restarting"
    assert serialized["last_seen"] == T0.isoformat()


def test_filter_events_by_namespace():
    events = [_event("BackOff", namespace="obs"), _event("BackOff", namespace="other")]

    result = filter_events(events, namespace="obs")

    assert len(result) == 1
    assert result[0]["involved_object"]["namespace"] == "obs"


def test_filter_events_by_kind_and_name():
    events = [
        _event("NodeNotReady", kind="Node", name="k8s-worker1"),
        _event("BackOff", kind="Pod", name="backend-1"),
    ]

    result = filter_events(events, kind="Node", name="k8s-worker1")

    assert len(result) == 1
    assert result[0]["reason"] == "NodeNotReady"


def test_filter_events_by_reason():
    events = [_event("BackOff"), _event("FailedScheduling")]

    result = filter_events(events, reason="FailedScheduling")

    assert len(result) == 1


def test_filter_events_by_severity():
    events = [_event("FailedScheduling"), _event("BackOff"), _event("Started", event_type="Normal")]

    result = filter_events(events, severity="critical")

    assert len(result) == 1
    assert result[0]["reason"] == "FailedScheduling"


def test_filter_events_combines_filters_with_and_semantics():
    events = [
        _event("BackOff", namespace="obs", name="backend-1"),
        _event("BackOff", namespace="obs", name="backend-2"),
    ]

    result = filter_events(events, namespace="obs", name="backend-1")

    assert len(result) == 1
    assert result[0]["involved_object"]["name"] == "backend-1"


def test_filter_events_no_filters_returns_everything():
    events = [_event("BackOff"), _event("FailedScheduling")]

    assert filter_events(events) == events
