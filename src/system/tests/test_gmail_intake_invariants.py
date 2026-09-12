"""Invariant over generated mailbox intakes: reading never becomes acting.

Reading a mailbox must never approve a review, record a draft intent, or call the draft
provider, whatever the mailbox contains or how often it is read. Stated over a generated
family rather than one example, because the single named case only covers the mailbox its
author pictured.
"""

import base64
import itertools
import json

from communications.controlled import ControlledDrafts
from communications.gmail import GmailCredentials, GmailReader
from data import store
from data.repository import Repository
from recruiting.models import Profile
from system.workflow import Workflow

MAILBOX = "operator@example.com"
# Bodies chosen to exercise every disposition intake can reach: a job that advances, one
# that does not, one that is unscored, and a message with nothing extractable at all.
JOBS = {
    "advances": "Title: Application Engineer\r\nCompany: Example Company\r\n"
    "Location: remote\r\nSkills: Python, SQL\r\nURL: https://jobs.example.com/roles/1\r\n",
    "below_threshold": "Title: Analyst\r\nCompany: Example Company\r\n"
    "Location: remote\r\nSkills: Python, Rust, Go, Java, Scala\r\n"
    "URL: https://jobs.example.com/roles/2\r\n",
    "ineligible": "Title: Engineer\r\nCompany: Example Company\r\n"
    "Location: onsite New York\r\nSkills: Python, SQL\r\n"
    "URL: https://jobs.example.com/roles/3\r\n",
    "unscored": "Title: Generalist\r\nCompany: Example Company\r\n"
    "Location: remote\r\nURL: https://jobs.example.com/roles/4\r\n",
    "unreadable": "Nothing here names a job.\r\n",
    "two_jobs": "Title: One\r\nCompany: Example Company\r\nLocation: remote\r\n"
    "Skills: Python, SQL\r\nURL: https://jobs.example.com/roles/5\r\n"
    "Title: Two\r\nCompany: Example Company\r\nLocation: remote\r\n"
    "Skills: Python\r\nURL: https://jobs.example.com/roles/6\r\n",
}
PROFILES = (
    Profile(("python", "sql")),
    Profile(("python",)),
    Profile(("python", "sql", "aws", "terraform", "kubernetes")),
    Profile(("python", "sql"), ("onsite new york",)),
)
# Every mailbox of one or two of those bodies, plus one of all of them.
MAILBOX_SHAPES = (
    [(name,) for name in JOBS] + [pair for pair in itertools.combinations(JOBS, 2)] + [tuple(JOBS)]
)


def raw(kind):
    return (
        f"From: alerts@example.com\r\nTo: {MAILBOX}\r\nSubject: {kind}\r\n"
        f"Message-ID: <{kind}@example.com>\r\nMIME-Version: 1.0\r\n"
        'Content-Type: text/plain; charset="utf-8"\r\n\r\n'
        f"{JOBS[kind]}"
    ).encode()


def reader(shape):
    messages = {f"id{n}": raw(kind) for n, kind in enumerate(shape)}

    def transport(url, headers):
        if url.endswith("/profile"):
            payload = {"emailAddress": MAILBOX}
        elif "/messages/" in url:
            identifier = url.split("/messages/", 1)[1].split("?", 1)[0]
            payload = {
                "id": identifier,
                "raw": base64.urlsafe_b64encode(messages[identifier]).decode().rstrip("="),
            }
        else:
            payload = {"messages": [{"id": i} for i in messages]}
        return 200, json.dumps(payload).encode()

    return GmailReader(GmailCredentials("synthetic-token", MAILBOX), transport)


def acted(path):
    """Every trace an approval, a draft intent or a provider write would leave."""
    with store.connection(path) as conn:
        decisions = conn.execute("SELECT count(*) FROM decisions").fetchone()[0]
        intents = conn.execute("SELECT count(*) FROM draft_intents").fetchone()[0]
        events = {
            row[0]
            for row in conn.execute("SELECT DISTINCT event FROM audit").fetchall()
            if row[0] != "review_created"
        }
    return decisions, intents, events


def test_reading_a_mailbox_never_approves_drafts_or_calls_the_provider(tmp_path):
    failures = []
    for index, (shape, profile) in enumerate(itertools.product(MAILBOX_SHAPES, PROFILES)):
        path = tmp_path / f"db{index}"
        provider = ControlledDrafts()
        flow = Workflow(Repository(path), provider, profile)
        for message in reader(shape).messages():
            try:
                flow.intake_message(message)
            except ValueError:
                # A refused message is still a read that must have written no action.
                pass
        # Read twice: a replay must not act either.
        for message in reader(shape).messages():
            flow.intake_message(message)
        decisions, intents, events = acted(path)
        if decisions or intents or events or provider.calls:
            failures.append((shape, profile.skills, decisions, intents, events, provider.calls))
    total = len(MAILBOX_SHAPES) * len(PROFILES)
    assert not failures, f"{len(failures)} of {total}, first: {failures[:3]}"
    assert total > 50, f"family too small to be worth running: {total}"


def test_a_drafted_review_still_requires_an_explicit_decision_afterwards(tmp_path):
    """The invariant above must not hold merely because nothing was ever draftable."""
    path = tmp_path / "db"
    provider = ControlledDrafts()
    flow = Workflow(Repository(path), provider, PROFILES[0])
    reviews = []
    for message in reader(("advances",)).messages():
        reviews.extend(flow.intake_message(message))
    assert reviews, "no review was created; the invariant above would be vacuous"
    flow.repository.decide(reviews[0], approved=True, actor="synthetic-operator")
    assert flow.draft(reviews[0])
    assert provider.calls == 1
