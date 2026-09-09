"""Read-only operator queries over opportunities."""

import json

import pytest

from data import store
from data.repository import Repository
from recruiting.status import STATUSES

JOBS = [
    ("IAM Architect", "Example Corp", "1", "remote", ["python", "sql", "iam"]),
    ("SailPoint Engineer", "Contoso", "2", "remote", ["sailpoint", "iam", "python", "terraform"]),
    ("Desktop Support", "Fabrikam", "3", "onsite New York", ["windows", "helpdesk", "python"]),
    ("Generalist", "Northwind", "4", "remote", []),
]


def seeded(path):
    from communications.controlled import ControlledDrafts
    from recruiting.models import Profile
    from system.workflow import Workflow

    repository = Repository(path)
    flow = Workflow(repository, ControlledDrafts(), Profile(("python", "sql", "iam", "sailpoint")))
    flow.intake(
        "message-1",
        json.dumps(
            {
                "jobs": [
                    {
                        "title": title,
                        "company": company,
                        "url": f"https://jobs.example.com/{path_part}",
                        "location": location,
                        "skills": skills,
                    }
                    for title, company, path_part, location, skills in JOBS
                ]
            }
        ),
    )
    for row, status in zip(repository.opportunities(), ("reviewing", "interested", "rejected")):
        repository.record_status(row["id"], status, actor="operator")
    return repository


def test_rows_are_ordered_by_pipeline_stage_then_coverage_then_company(tmp_path):
    rows = seeded(tmp_path / "db").opportunities()
    order = [STATUSES.index(row["status"]) for row in rows]
    assert order == sorted(order), [row["status"] for row in rows]
    for first, second in zip(rows, rows[1:]):
        if first["status"] == second["status"]:
            assert (first["coverage"] or -1) >= (second["coverage"] or -1)


@pytest.mark.parametrize("status", STATUSES)
def test_filtering_by_status_returns_only_that_status(tmp_path, status):
    rows = seeded(tmp_path / "db").opportunities(status=status)
    assert all(row["status"] == status for row in rows)


def test_active_returns_only_searches_in_progress(tmp_path):
    rows = seeded(tmp_path / "db").opportunities(active=True)
    assert {row["status"] for row in rows} == {"reviewing", "interested"}


def test_eligibility_partitions_the_whole_set(tmp_path):
    repository = seeded(tmp_path / "db")
    every = repository.opportunities()
    yes = repository.opportunities(eligible=True)
    no = repository.opportunities(eligible=False)
    assert all(row["eligible"] for row in yes)
    assert not any(row["eligible"] for row in no)
    assert {row["id"] for row in yes} | {row["id"] for row in no} == {row["id"] for row in every}
    assert not {row["id"] for row in yes} & {row["id"] for row in no}


def test_coverage_bounds_are_inclusive_and_composable(tmp_path):
    repository = seeded(tmp_path / "db")
    covered = [row["coverage"] for row in repository.opportunities() if row["coverage"]]
    lowest = min(covered)
    assert all(row["coverage"] >= lowest for row in repository.opportunities(min_coverage=lowest))
    assert any(row["coverage"] == lowest for row in repository.opportunities(min_coverage=lowest))
    assert all(row["coverage"] <= lowest for row in repository.opportunities(max_coverage=lowest))
    exact = repository.opportunities(min_coverage=lowest, max_coverage=lowest)
    assert exact and all(row["coverage"] == lowest for row in exact)


REFUSED_FILTERS = [
    {"status": "reopened"},
    {"status": ""},
    {"status": 17},
    {"min_coverage": -1},
    {"min_coverage": 101},
    {"min_coverage": "50"},
    {"min_coverage": 50.0},
    {"min_coverage": True},
    {"max_coverage": -1},
    {"max_coverage": 101},
    {"max_coverage": "50"},
    {"eligible": "yes"},
    {"eligible": 1},
]


@pytest.mark.parametrize("filters", REFUSED_FILTERS)
def test_a_filter_that_cannot_mean_anything_is_refused(tmp_path, filters):
    repository = seeded(tmp_path / "db")
    with pytest.raises(ValueError):
        repository.opportunities(**filters)


def test_an_unscored_opportunity_is_distinguishable_from_a_covered_one(tmp_path):
    rows = {row["title"]: row for row in seeded(tmp_path / "db").opportunities()}
    assert rows["Generalist"]["scored"] is False
    assert rows["Generalist"]["coverage"] is None
    assert rows["Generalist"]["actionable"] is True
    assert rows["IAM Architect"]["scored"] is True


def test_a_review_written_before_coverage_is_reported_as_not_actionable(tmp_path):
    """The operator has to see that such a review cannot be approved or drafted."""
    path = tmp_path / "db"
    repository = seeded(path)
    # Chosen by title, not by position: pipeline order puts the unscored "new" row first,
    # and an earlier draft of this test rewrote the row it then used as the contrast.
    target = next(r for r in repository.opportunities() if r["title"] == "IAM Architect")
    with store.connection(path) as conn, store.transaction(conn):
        conn.execute(
            "UPDATE reviews SET stated_skills=NULL,matched_skills=NULL,coverage=NULL WHERE id=?",
            (target["review"],),
        )
    row = {r["id"]: r for r in repository.opportunities()}[target["id"]]
    assert row["actionable"] is False
    assert row["scored"] is False
    assert row["coverage"] is None
    # A scoring-v2 review of a job that states no skills is a different thing and must not
    # be reported the same way.
    unscored = next(r for r in repository.opportunities() if r["title"] == "Generalist")
    assert unscored["actionable"] is True and unscored["scored"] is False


def test_the_detail_view_carries_the_packet_and_the_whole_history(tmp_path):
    repository = seeded(tmp_path / "db")
    first = repository.opportunities()[0]
    record = repository.opportunity(first["id"])
    assert record["packet"]["reasons"] and record["packet"]["draft"]
    assert [event["status"] for event in record["history"]][0] == "new"
    assert record["history"][-1]["status"] == record["status"]
    assert record["company"] == first["company"]


def test_the_detail_history_is_ordered_by_event_not_by_clock(tmp_path):
    """The detail view has its own history query, so it needs its own proof.

    Mutating the list query's ordering was caught; mutating this one was not, because no
    test made the two orderings disagree here.
    """
    path = tmp_path / "db"
    repository = seeded(path)
    target = next(r for r in repository.opportunities() if r["title"] == "IAM Architect")
    written = (("interested", 5000), ("applied", 1000), ("interviewing", 3000))
    with store.connection(path) as conn, store.transaction(conn):
        for status, stamp in written:
            conn.execute(
                "INSERT INTO opportunity_status_history"
                "(opportunity_id,status,actor,created_at) VALUES (?,?,'operator',?)",
                (target["id"], status, stamp),
            )
    record = repository.opportunity(target["id"])
    assert [event["status"] for event in record["history"]][-3:] == [s for s, _ in written]
    assert record["history"][-1]["status"] == record["status"] == "interviewing"


def test_an_unknown_opportunity_is_a_key_error(tmp_path):
    with pytest.raises(KeyError):
        seeded(tmp_path / "db").opportunity("missing")


def test_an_opportunity_without_a_current_review_still_lists(tmp_path):
    """A row the reviews join cannot satisfy must appear, not vanish from the operator's list."""
    path = tmp_path / "db"
    repository = seeded(path)
    with store.connection(path) as conn, store.transaction(conn):
        conn.execute(
            "INSERT INTO opportunities(id,url,title,company,location) "
            "VALUES ('bare','https://jobs.example.com/bare','Bare','Adventure Works','remote')"
        )
    rows = {row["id"]: row for row in repository.opportunities()}
    assert "bare" in rows
    bare = rows["bare"]
    assert bare["review"] is None and bare["coverage"] is None
    assert bare["eligible"] is None and bare["scored"] is None and bare["actionable"] is None
    assert bare["status"] is None
