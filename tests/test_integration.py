"""Integration tests that exercise the real database layer (#4).

Skipped automatically when no reachable DB is configured (e.g. local runs without
DATABASE_URL). CI provides a Postgres service so these run there.
"""

from __future__ import annotations

import uuid

import pytest

from app import db


def _db_available() -> bool:
    try:
        return db.enabled() and db.ping()
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _db_available(), reason="no reachable database")


@pytest.fixture(scope="module", autouse=True)
def _schema():
    db.init_schema()
    yield


def test_handoff_record_and_list():
    cid = f"it-{uuid.uuid4().hex[:8]}"
    db.record_handoff(cid, "Ada", "dispute", "why was I charged")
    ids = [h["conversation_id"] for h in db.list_handoffs(limit=100)]
    assert cid in ids


def test_outcome_stats_and_revenue():
    cid = f"it-{uuid.uuid4().hex[:8]}"
    db.record_outcome(cid, "recovered", amount=1234.0)
    stats = db.outcome_stats()
    assert stats["enabled"] and stats["total"] >= 1
    assert stats["revenue_recovered"] >= 1234.0


def test_event_idempotency():
    evt = f"evt-{uuid.uuid4().hex[:8]}"
    assert db.already_processed(evt) is False
    db.mark_processed(evt)
    assert db.already_processed(evt) is True
    db.mark_processed(evt)  # second time is a no-op, must not raise


def test_contacts_map_phone_to_latest_conversation():
    phone = f"234{uuid.uuid4().int % 10**10:010d}"
    db.upsert_contact(phone, "conv-a")
    db.upsert_contact(phone, "conv-b")
    assert db.conversation_for_phone(phone) == "conv-b"


def test_retry_queue_claims_due_once_and_supersedes():
    from datetime import datetime, timedelta, timezone

    cid = f"it-{uuid.uuid4().hex[:8]}"
    past = datetime.now(timezone.utc) - timedelta(minutes=1)
    db.schedule_retry(cid, past + timedelta(days=1))
    db.schedule_retry(cid, past)  # supersedes the first; only this one is pending
    assert db.pending_retry(cid) is not None
    claimed = [r for r in db.claim_due_retries(limit=100) if r[1] == cid]
    assert len(claimed) == 1
    assert not [r for r in db.claim_due_retries(limit=100) if r[1] == cid]  # not twice
    db.finish_retry(claimed[0][0], "done")
    assert db.pending_retry(cid) is None


def test_release_event_allows_reprocessing():
    evt = f"evt-{uuid.uuid4().hex[:8]}"
    assert db.claim_event(evt) is True
    db.release_event(evt)
    assert db.claim_event(evt) is True


def test_claim_event_is_first_delivery_only():
    evt = f"evt-{uuid.uuid4().hex[:8]}"
    assert db.claim_event(evt) is True
    assert db.claim_event(evt) is False


def test_conversation_cost_reads_persisted_calls():
    cid = f"it-{uuid.uuid4().hex[:8]}"
    db.record_llm_call({"conversation_id": cid, "provider": "anthropic", "model": "m",
                        "prompt_tokens": 10, "completion_tokens": 5,
                        "latency_ms": 1.0, "cost_usd": 0.02})
    assert db.conversation_cost(cid) == pytest.approx(0.02)


def test_conversation_lock_is_exclusive():
    cid = f"it-{uuid.uuid4().hex[:8]}"
    with db.conversation_lock(cid) as first:
        assert first is True
        with db.conversation_lock(cid) as second:
            assert second is False  # already held
    # released now — reacquire succeeds
    with db.conversation_lock(cid) as again:
        assert again is True


def test_save_and_load_conversation_roundtrip():
    cid = f"it-{uuid.uuid4().hex[:8]}"
    db.save_conversation(cid, {"history": [{"role": "agent", "content": "hi"}], "promises": ["Friday"]})
    loaded = db.load_conversation(cid)
    assert loaded["promises"] == ["Friday"]
