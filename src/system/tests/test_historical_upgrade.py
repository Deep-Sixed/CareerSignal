"""A database written under an older schema, opened by the CareerSignal that exists now.

Every migration test beside this one starts from nothing: it creates a fresh database,
applies all seven migrations, and asserts the result. That proves the SQL executes. It
cannot prove the thing an operator actually depends on, which is that a database holding
months of their decisions still means what it meant after the upgrade runs over it.

So this builds a genuine 0003-era database -- the real historical migration resources,
applied by the real runner, carrying rows written the way that schema allowed -- and then
lets the current code open it. What is asserted afterwards is semantic rather than
structural:

    old facts survive                     an opportunity recorded then is readable now
    0004 backfills honestly               the row it writes says a migration wrote it
    0005 does not fabricate authority     an old approval no longer authorizes a draft
    0006 does not fabricate a target      no addressing is invented for one
    0007 preserves the one-attempt rule   an old intent still bars a second attempt

The three "does not fabricate" claims are the reason this file exists. Migrations 0005,
0006 and 0007 each added columns that bind an approval to what the operator read, and each
deliberately left old rows with values that cannot match. That is a safety decision: the
database has no record of what those approvals were for, so claiming they bind to the
current review would be an invention. A migration that quietly backfilled a plausible
value instead would authorize an outward write nobody approved -- and every structural
test in this repository would still pass.

No fixture database is committed. A checked-in `.db` is a binary nobody reviews, and it
would drift from the migrations it claims to predate; built here, the old schema is
whatever 0001 through 0003 actually say it is.
"""

import json

import pytest

from communications.controlled import ControlledDrafts
from data import store
from data.repository import Repository
from system.workflow import OutwardActions

# The last migration that existed before status history, approval binding, addressing and
# provider identity arrived. Everything this file calls "old" was written under it.
ERA = "0003_stated_skill_coverage.sql"

MESSAGE = "message-0003-era"
OPPORTUNITY = "opportunity-0003-era"
REVIEW = "review-0003-era"
APPROVED_OPPORTUNITY = "opportunity-0003-era-approved"
APPROVED_REVIEW = "review-0003-era-approved"
ATTEMPTED_OPPORTUNITY = "opportunity-0003-era-attempted"
ATTEMPTED_REVIEW = "review-0003-era-attempted"

# Each review is the current review of its own opportunity. That is not tidiness: an
# authorization check reads the binding through `opportunities.current_review`, so a review
# that is not current refuses as stale before any of the columns this file is about are
# consulted -- and every assertion below would pass while proving something else. The first
# draft of this fixture had exactly that defect.
OPPORTUNITIES = {
    OPPORTUNITY: REVIEW,
    APPROVED_OPPORTUNITY: APPROVED_REVIEW,
    ATTEMPTED_OPPORTUNITY: ATTEMPTED_REVIEW,
}


def migrate_through(monkeypatch, path, last):
    """Apply the real migrations up to `last`, using the real runner.

    Monkeypatching the file list rather than re-implementing the apply loop is what keeps
    this honest: the old database is built by the same code that builds a new one, so it
    carries the same digests in `schema_migrations` and the later run treats it exactly as
    it would treat a real operator's database rather than as a special case.
    """
    everything = store.migration_files()
    monkeypatch.setattr(store, "migration_files", lambda: [p for p in everything if p.name <= last])
    applied = store.migrate(path)
    monkeypatch.undo()
    return applied


def write_era_rows(path):
    """Rows as the 0003 schema allowed them: no history, no binding, no destination.

    Written through `store.connection` and plain SQL rather than through `Repository`,
    because `Repository` is current code and would insist on columns this schema does not
    have. An operator's old database was written by old code, and that is what this is.
    """
    with store.connection(path) as conn, store.transaction(conn):
        conn.execute("INSERT INTO messages VALUES (?,?)", (MESSAGE, "digest-of-the-old-message"))
        conn.execute(
            "INSERT INTO message_sources VALUES (?,?,?,?,?,?,?)",
            (
                MESSAGE,
                "inbox",
                "external-0003",
                "recruiter@example.com",
                "A role from before the upgrade",
                "text",
                "parser-0003",
            ),
        )
        for index, (opportunity, review) in enumerate(OPPORTUNITIES.items()):
            conn.execute(
                "INSERT INTO opportunities VALUES (?,?,?,?,?,?)",
                (
                    opportunity,
                    f"https://jobs.example.com/old-role-{index}",
                    "Staff Engineer",
                    "Contoso Analytics",
                    "Remote",
                    review,
                ),
            )
            conn.execute(
                "INSERT INTO reviews VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    review,
                    opportunity,
                    f"content-digest-{review}",
                    90,
                    1,
                    json.dumps({"title": "Staff Engineer", "company": "Contoso Analytics"}),
                    "The draft wording the operator read at the time.",
                    json.dumps(["python"]),
                    json.dumps(["python"]),
                    100,
                ),
            )
        # One message carried all three, which is ordinary: provenance is keyed by
        # (message, opportunity), and addressing is derived through it. Without a row here a
        # binding cannot be computed at all, and the refusals below would be about that.
        for opportunity, review in OPPORTUNITIES.items():
            conn.execute("INSERT INTO provenance VALUES (?,?,?)", (MESSAGE, opportunity, review))
        conn.execute(
            "INSERT INTO extraction_items VALUES (?,?,?,?,?,?)",
            (MESSAGE, 0, "", "", OPPORTUNITY, REVIEW),
        )
        # An approval recorded under the old contract: approved, by a named operator, and
        # carrying nothing about what was approved -- because there was nowhere to put it.
        conn.execute("INSERT INTO decisions VALUES (?,?,?)", (APPROVED_REVIEW, 1, "operator"))
        # An approval that already produced an attempt, settled before provider identity
        # existed. The receipt is real; what it was addressed to was never recorded.
        conn.execute("INSERT INTO decisions VALUES (?,?,?)", (ATTEMPTED_REVIEW, 1, "operator"))
        conn.execute(
            "INSERT INTO draft_intents VALUES (?,?,?)",
            (ATTEMPTED_REVIEW, "confirmed", "receipt-from-before-provider-identity"),
        )
        conn.execute(
            "INSERT INTO audit(review_id,event) VALUES (?,?)", (APPROVED_REVIEW, "approved")
        )


@pytest.fixture
def era_database(tmp_path, monkeypatch):
    """A 0003-era database with 0003-era rows, not yet upgraded."""
    path = tmp_path / "operator.db"
    applied = migrate_through(monkeypatch, path, ERA)
    assert applied[-1] == ERA, applied
    write_era_rows(path)
    return path


def columns(path, table):
    with store.connection(path) as conn:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def tables(path):
    with store.connection(path) as conn:
        return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_the_era_database_really_predates_what_this_file_tests(era_database):
    """The vacuity guard, and the reason it is the first test in the file.

    Everything below is a claim about upgrading. If the fixture quietly built a current
    database instead -- a rename, a reordered filename, a monkeypatch that stopped
    applying -- every later assertion would pass while testing nothing at all, because a
    current database trivially has current columns.

    So the old database is asserted to be genuinely old before anything is upgraded over
    it. A green result is only evidence when the experiment reached the condition it
    claims to be about.
    """
    assert "opportunity_status_history" not in tables(era_database)
    assert not {"content_digest", "draft_digest", "status_event_id"} & columns(
        era_database, "decisions"
    )
    assert not {"addressing_digest", "source_message_id"} & columns(era_database, "decisions")
    assert not {"provider", "provider_namespace"} & columns(era_database, "decisions")
    assert not {"provider", "provider_namespace"} & columns(era_database, "draft_intents")

    # And the reviews under test are each current for their own opportunity. An
    # authorization check reads its binding through `opportunities.current_review`, so a
    # stale review refuses before any binding column is consulted. The first draft of this
    # fixture shared one opportunity between all three reviews, and the outward tests below
    # passed on staleness while claiming to be about migrations 0005 to 0007 -- green, and
    # about the wrong thing entirely.
    with store.connection(era_database) as conn:
        current = dict(conn.execute("SELECT id, current_review FROM opportunities"))
    assert current == OPPORTUNITIES, current


def test_a_0003_era_database_upgrades_through_every_later_migration(era_database):
    """The upgrade runs, applies exactly what was missing, and leaves a whole contract."""
    assert store.migrate(era_database) == [
        "0004_status_history.sql",
        "0005_draft_authorization.sql",
        "0006_addressing_authorization.sql",
        "0007_provider_identity.sql",
    ]
    # Integrity, foreign keys and the full migration digest set, from the tool that decides
    # whether a database is one this build may use at all.
    store.verify_contract(era_database)
    assert store.migrate(era_database) == []


def test_the_facts_recorded_before_the_upgrade_all_survive_it(era_database):
    """An operator's history is the thing an upgrade must not cost them."""
    store.migrate(era_database)
    repository = Repository(era_database)

    found = repository.opportunity(OPPORTUNITY)
    assert found["title"] == "Staff Engineer"
    assert found["company"] == "Contoso Analytics"
    assert found["url"] == "https://jobs.example.com/old-role-0"

    # Provenance and the extracted item still connect the message to what came out of it.
    assert [row[0] for row in repository.extraction_evidence(MESSAGE)] == [0]
    arrived_in = repository.sources(OPPORTUNITY)
    assert [source["message"] for source in arrived_in] == [MESSAGE], arrived_in
    assert arrived_in[0]["sender"] == "recruiter@example.com"

    # And the audit trail written under the old schema is still there to read.
    assert repository.audit(APPROVED_REVIEW) == ["approved"]


def test_the_backfill_says_a_migration_wrote_it_not_an_operator(era_database):
    """0004 gives old opportunities a history without claiming somebody was there.

    The distinction is the whole value of the row. A backfilled `new` attributed to an
    operator would be a record of a decision that nobody made, and every later reading of
    that history -- how long a thing sat, who moved it -- would be built on it.
    """
    store.migrate(era_database)
    repository = Repository(era_database)

    # Every opportunity that existed gets exactly one opening event -- not none, which
    # would leave it with no history at all, and not several.
    for opportunity in OPPORTUNITIES:
        history = repository.status_history(opportunity)
        assert len(history) == 1, (opportunity, history)
        status, actor, reason, _recorded_at = history[0]
        assert status == "new"
        assert actor == "migration", "the backfill claims an operator recorded this"
        assert "backfill" in reason.casefold(), reason


def test_an_approval_from_before_0005_no_longer_authorizes_a_draft(era_database):
    """The claim this file exists for, asserted on a provider call count.

    The old decision still says `approved`, by a named operator. What it cannot say is what
    was approved, and 0005, 0006 and 0007 each left their columns empty rather than guess.
    The result has to be a refusal: an approval that predates the binding is not a weaker
    authorization, it is not an authorization for this action at all.

    Asserted by counting provider calls rather than by reading a message, because a refusal
    and a rejection read much the same to a person and only the call count can say whether
    anything was contacted.
    """
    store.migrate(era_database)
    repository = Repository(era_database)
    provider = ControlledDrafts()
    actions = OutwardActions(repository, provider)

    with pytest.raises(ValueError, match="Approval predates draft authorization binding"):
        actions.draft(APPROVED_REVIEW)

    assert provider.calls == 0, "a provider was contacted on an approval that cannot bind"
    assert repository.intent(APPROVED_REVIEW) is None, "an intent was reserved for it"


def test_no_addressing_or_destination_is_invented_for_an_old_approval(era_database):
    """0006 and 0007 leave their columns empty, which is the honest value.

    Checked on the row rather than only through behaviour, because "empty" is the specific
    thing that makes the refusal above correct: a plausible-looking default here would
    authorize an outward write to a destination nobody named.
    """
    store.migrate(era_database)
    with store.connection(era_database) as conn:
        row = conn.execute(
            "SELECT content_digest, draft_digest, status_event_id, addressing_digest,"
            " source_message_id, provider, provider_namespace FROM decisions WHERE review_id=?",
            (APPROVED_REVIEW,),
        ).fetchone()
    content, draft, status_event, addressing, source, provider, namespace = row
    assert (content, draft, addressing, provider, namespace) == ("", "", "", "", "")
    assert status_event == 0
    assert source is None, "a source message was invented for an approval that had none"


def test_an_intent_from_before_0007_still_bars_a_second_attempt(era_database):
    """One review, one outward attempt -- a rule the upgrade must not loosen.

    The old intent is `confirmed` with a real receipt and no recorded destination. The
    upgrade must neither discard it, which would free the review for a second draft, nor
    dress it in a destination it never had.
    """
    store.migrate(era_database)
    repository = Repository(era_database)
    provider = ControlledDrafts()
    actions = OutwardActions(repository, provider)

    assert repository.intent(ATTEMPTED_REVIEW) == (
        "confirmed",
        "receipt-from-before-provider-identity",
    )

    with pytest.raises(
        ValueError, match="Requested provider does not match the draft intent already reserved"
    ):
        actions.draft(ATTEMPTED_REVIEW)

    assert provider.calls == 0, "a second attempt reached a provider"
    assert repository.intent(ATTEMPTED_REVIEW) == (
        "confirmed",
        "receipt-from-before-provider-identity",
    ), "the settled attempt was altered by a request it should have refused"
