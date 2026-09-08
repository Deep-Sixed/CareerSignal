import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from communications.controlled import ControlledDrafts
from data.repository import Repository
from data.store import connection
from recruiting.models import Profile
from system.demo import golden_workflow
from system.workflow import Workflow


def body(**changes):
    job = dict(
        title="Engineer",
        company="Example Company",
        url="https://jobs.example.com/1",
        location="remote",
        skills=["python", "sql"],
    )
    job.update(changes)
    return json.dumps({"jobs": [job, job]})


@pytest.fixture
def flow(tmp_path):
    return Workflow(Repository(tmp_path / "db"), ControlledDrafts(), Profile(("python", "sql")))


def test_golden_workflow_and_restart(tmp_path):
    first = golden_workflow(tmp_path / "db")
    assert golden_workflow(tmp_path / "db") == first


def test_multiple_jobs_in_one_message_replay_in_stable_order(flow):
    jobs = [json.loads(body(url=f"https://jobs.example.com/{i}"))["jobs"][0] for i in range(4)]
    message = json.dumps({"jobs": jobs})
    first = flow.intake("multiple", message)
    assert len(first) == 4
    assert flow.intake("multiple", message) == first


def test_message_and_opportunity_dedupe_and_provenance(flow):
    first = flow.intake("one", body())
    assert len(first) == 1
    assert flow.intake("one", body()) == first
    assert flow.intake("two", body()) == first
    with connection(flow.repository.path) as conn:
        assert conn.execute("SELECT count(*) FROM provenance").fetchone()[0] == 2
        assert conn.execute("SELECT count(*) FROM opportunities").fetchone()[0] == 1
    with pytest.raises(ValueError, match="different content"):
        flow.intake("one", body(title="Changed"))


def test_approval_rejection_and_ineligible_boundaries(flow):
    review = flow.intake("one", body())[0]
    with pytest.raises(ValueError, match="approval"):
        flow.draft(review)
    flow.repository.decide(review, approved=False, actor="operator")
    with pytest.raises(ValueError):
        flow.draft(review)
    for i, fields in enumerate((dict(location="onsite"), dict(skills=["python"]))):
        rejected = flow.intake(f"reject{i}", body(**fields))[0]
        with pytest.raises(ValueError):
            flow.repository.decide(rejected, approved=True, actor="operator")
    assert flow.provider.calls == 0


def test_changed_content_invalidates_old_approval_even_when_reverted(flow):
    old = flow.intake("one", body())[0]
    flow.repository.decide(old, approved=True, actor="operator")
    flow.intake("two", body(title="Changed"))
    with pytest.raises(ValueError, match="stale"):
        flow.draft(old)
    reverted = flow.intake("three", body())[0]
    assert reverted != old
    with pytest.raises(ValueError, match="approval"):
        flow.draft(reverted)


def test_two_workers_only_one_provider_call(flow):
    review = flow.intake("one", body())[0]
    flow.repository.decide(review, approved=True, actor="operator")
    with ThreadPoolExecutor() as pool:
        list(pool.map(lambda _: flow.draft(review), range(2)))
    assert flow.provider.calls == 1
    assert flow.repository.intent(review)[0] == "confirmed"


@pytest.mark.parametrize("after_success", [False, True])
def test_uncertain_external_result_never_blindly_retries(flow, after_success):
    review = flow.intake("one", body())[0]
    flow.repository.decide(review, approved=True, actor="operator")
    original = flow.provider.create

    def fail(key, text):
        if after_success:
            original(key, text)
        raise OSError("controlled interruption")

    flow.provider.create = fail
    with pytest.raises(OSError):
        flow.draft(review)
    assert flow.repository.intent(review)[0] == "uncertain"
    assert flow.draft(review) is None
    assert bool(flow.reconcile(review)) == after_success
    assert flow.provider.calls == int(after_success)


def test_failure_to_persist_receipt_can_be_reconciled(flow, monkeypatch):
    review = flow.intake("one", body())[0]
    flow.repository.decide(review, approved=True, actor="operator")
    original = flow.repository.finish

    def fail(*args):
        raise OSError("disk unavailable")

    monkeypatch.setattr(flow.repository, "finish", fail)
    with pytest.raises(OSError):
        flow.draft(review)
    monkeypatch.setattr(flow.repository, "finish", original)
    assert flow.repository.intent(review)[0] == "attempting"
    assert flow.draft(review) is None
    assert flow.reconcile(review)
    assert flow.provider.calls == 1


def mixed_body():
    def good(index):
        return dict(
            title=f"Engineer {index}",
            company="Example Company",
            url=f"https://jobs.example.com/{index}",
            location="remote",
            skills=["python", "sql"],
        )

    return json.dumps(
        {
            "jobs": [
                good(1),
                dict(good(2), url="http://insecure.example.com/2"),
                good(3),
                {"company": "Example Company", "url": "https://jobs.example.com/4"},
            ]
        }
    )


def test_structured_intake_keeps_valid_jobs_when_a_sibling_is_malformed(flow):
    reviews = flow.intake("mixed", mixed_body())
    assert len(reviews) == 2
    evidence = flow.repository.extraction_evidence("mixed")
    assert [row[0] for row in evidence] == [0, 1, 2, 3]
    assert [bool(row[4]) for row in evidence] == [True, False, True, False]
    assert sorted(row[4] for row in evidence if row[4]) == reviews
    # A rejected entry records why, and creates no opportunity or review.
    assert all(row[2] and row[3] is None for row in evidence if not row[4])


def test_structured_intake_replays_without_duplicating_evidence(flow):
    first = flow.intake("mixed", mixed_body())
    evidence = flow.repository.extraction_evidence("mixed")
    assert flow.intake("mixed", mixed_body()) == first
    assert flow.repository.extraction_evidence("mixed") == evidence


def test_a_malformed_envelope_still_fails_the_whole_message(flow):
    """A job we cannot read is one item; an envelope we cannot read is the message."""
    for body in ('{"jobs": []}', '{"jobs": {}}', "[]", "not json at all"):
        with pytest.raises(ValueError):
            flow.intake("bad", body)
    with connection(flow.repository.path) as conn:
        assert conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM extraction_items").fetchone()[0] == 0


def test_structured_intake_creates_no_drafts(flow):
    reviews = flow.intake("mixed", mixed_body())
    for review in reviews:
        with pytest.raises(ValueError, match="approval"):
            flow.draft(review)
    assert flow.provider.calls == 0
