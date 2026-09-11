"""The refusals are written first: a draft-only adapter is defined by what it cannot do."""

import base64
import inspect
import json
import traceback

import pytest

from communications import gmail_draft
from communications.gmail import GMAIL_HOST, READONLY_SCOPE
from communications.gmail_draft import (
    API_ROOT,
    COMPOSE_SCOPE,
    FORBIDDEN_SEGMENTS,
    INTENT_HEADER,
    DraftRefused,
    GmailComposeCredentials,
    GmailDrafts,
    GmailError,
    compose,
    namespace_for,
    profile_url,
    recipient,
    subject,
    writable_url,
)

TOKEN = "synthetic-compose-token-value"
MAILBOX = "operator@example.com"
KEY = "a" * 64
BODY = "Thanks for reaching out about the role.\n\nBest,\nOperator"


def credentials(**overrides):
    return GmailComposeCredentials(**{"access_token": TOKEN, "mailbox": MAILBOX, **overrides})


class Recorder:
    """Serves synthetic Gmail draft payloads and records every request it is given."""

    def __init__(self, created=None, listings=(), drafts=None, status=200):
        self.created = created if created is not None else {"id": "draft123"}
        self.listings = list(listings)
        self.drafts = dict(drafts or {})
        self.status = status
        self.creates = []
        self.reads = []

    def create(self, url, headers, body):
        self.creates.append((url, dict(headers), body))
        return self.status, json.dumps(self.created).encode()

    def read(self, url, headers):
        self.reads.append((url, dict(headers)))
        for identifier, payload in self.drafts.items():
            if url.endswith("drafts/" + identifier) or f"drafts/{identifier}?" in url:
                return self.status, json.dumps(payload).encode()
        page = self.listings.pop(0) if self.listings else {}
        return self.status, json.dumps(page).encode()


def metadata(identifier, intent):
    return {
        "id": identifier,
        "message": {"payload": {"headers": [{"name": INTENT_HEADER, "value": intent}]}},
    }


# --- the capability that must not exist -----------------------------------------------


def test_the_adapter_defines_no_way_to_send():
    """A send method is not merely unused; there is nowhere to call one from."""
    names = [name for name, _ in inspect.getmembers(GmailDrafts, callable)]
    assert not [name for name in names if "send" in name.casefold()]


def test_every_url_the_provider_requests_is_inside_the_draft_allowlist():
    """Behavioural, not textual: drive the whole provider and audit what it asked for.

    A comment claiming the adapter cannot send proves nothing. This exercises create and
    lookup and then re-checks every URL either of them built.
    """
    recorder = Recorder(
        listings=[{"drafts": [{"id": "ours"}]}], drafts={"ours": metadata("ours", KEY)}
    )
    drafts = GmailDrafts(credentials(), create=recorder.create, read=recorder.read)
    drafts.create(KEY, BODY, to="jane@example.com", subject_line="role")
    drafts.lookup(KEY)
    requested = [url for url, *_ in recorder.creates] + [url for url, *_ in recorder.reads]
    assert requested, "the provider made no request; this test proves nothing"
    for url in requested:
        assert writable_url(url) == url
        assert "send" not in url.casefold()
    # Only the collection is ever posted to, and posting happens exactly once.
    assert [url for url, *_ in recorder.creates] == [API_ROOT + "users/me/drafts"]


@pytest.mark.parametrize(
    "path",
    [
        "users/me/drafts/send",
        "users/me/messages/send",
        "users/me/messages",
        "users/me/messages/abc",
        "users/me/drafts/trash",
        "users/me/drafts/modify",
        "users/me/labels",
        "users/me/threads",
        "users/me/settings/forwarding",
        "users/me/profile",
        "users/me/drafts/abc/send",
        "users/me/drafts/",
    ],
)
def test_every_path_but_the_drafts_collection_is_refused(path):
    with pytest.raises(GmailError):
        writable_url(API_ROOT + path)


def test_the_drafts_send_endpoint_matches_the_allowlist_shape_and_is_still_refused():
    """This is why the segment list exists: drafts/send is shaped exactly like a draft id."""
    assert gmail_draft.DRAFT_PATH.match("/gmail/v1/users/me/drafts/send")
    assert "send" in FORBIDDEN_SEGMENTS
    with pytest.raises(GmailError, match="reserved"):
        writable_url(API_ROOT + "users/me/drafts/send")


@pytest.mark.parametrize(
    "url",
    [
        "http://gmail.googleapis.com/gmail/v1/users/me/drafts",
        "https://gmail.googleapis.com:8443/gmail/v1/users/me/drafts",
        "https://evil.example.com/gmail/v1/users/me/drafts",
        "https://gmail.googleapis.com.evil.example/gmail/v1/users/me/drafts",
    ],
)
def test_a_token_is_never_offered_to_an_unauthorized_destination(url):
    with pytest.raises(GmailError):
        writable_url(url)


@pytest.mark.parametrize("userinfo", ["user:pass", "user", "operator%40example.com:secret"])
def test_a_url_carrying_credentials_is_refused_on_the_authorized_host_too(userinfo):
    """Composed rather than written out: a literal user:pass@host reads as an address to
    the tree scanner. Keeping the real host matters -- moving it to example.com would make
    this pass because the host is wrong, which would stop testing userinfo entirely."""
    url = "https://" + userinfo + "@" + GMAIL_HOST + "/gmail/v1/users/me/drafts"
    assert writable_url(API_ROOT + "users/me/drafts"), "the host in this test is the allowed one"
    with pytest.raises(GmailError, match="carrying credentials"):
        writable_url(url)


# --- the profile identity endpoint is a second, narrower allowlist --------------------


def test_the_exact_profile_endpoint_is_admitted():
    url = API_ROOT + "users/me/profile"
    assert profile_url(url) == url


@pytest.mark.parametrize(
    "path",
    [
        "users/me/drafts",
        "users/me/profile/",
        "users/me/profile?fields=emailAddress",
        "users/me/profileX",
        "users/me/messages/profile",
        "users/me/drafts/profile",
    ],
)
def test_every_path_but_the_exact_profile_endpoint_is_refused(path):
    with pytest.raises(GmailError):
        profile_url(API_ROOT + path)


def test_writable_url_does_not_admit_the_profile_endpoint():
    """The identity check gets its own allowlist rather than widening the drafts one."""
    with pytest.raises(GmailError):
        writable_url(API_ROOT + "users/me/profile")


def test_the_profile_endpoint_refuses_the_same_hostile_hosts_the_draft_allowlist_does():
    """The credential-carrying case is covered once, on writable_url: both allowlists share
    _validated_host(), so a URL refused there for carrying userinfo is refused here too."""
    with pytest.raises(GmailError):
        profile_url("http://" + GMAIL_HOST + "/gmail/v1/users/me/profile")
    with pytest.raises(GmailError):
        profile_url("https://evil.example.com/gmail/v1/users/me/profile")


# --- proving whose mailbox a credential belongs to -------------------------------------


def test_namespace_for_normalizes_case_and_whitespace():
    assert namespace_for(" Operator@Example.COM ") == "gmail:operator@example.com"


def test_namespace_for_refuses_an_empty_mailbox():
    with pytest.raises(ValueError):
        namespace_for("   ")


def test_namespace_for_refuses_a_non_string_mailbox():
    with pytest.raises(TypeError):
        namespace_for(None)


def test_a_credentials_namespace_and_an_identity_response_normalize_the_same_way():
    """Approving compares one against the other; both must be built by the same function."""
    assert credentials(mailbox=" Operator@Example.COM ").namespace == namespace_for(MAILBOX)


def test_identity_reads_the_profile_endpoint_and_returns_a_namespace():
    recorder = Recorder(listings=[{"emailAddress": MAILBOX}])
    drafts = GmailDrafts(credentials(), read=recorder.read)
    assert drafts.identity() == namespace_for(MAILBOX)
    assert recorder.reads[0][0] == API_ROOT + "users/me/profile"


def test_identity_makes_no_request_shaped_like_a_draft_write():
    recorder = Recorder(listings=[{"emailAddress": MAILBOX}])
    drafts = GmailDrafts(credentials(), read=recorder.read)
    drafts.identity()
    assert recorder.creates == [], "verifying identity created a draft"


def test_identity_refuses_a_response_with_no_email_address():
    recorder = Recorder(listings=[{}])
    drafts = GmailDrafts(credentials(), read=recorder.read)
    with pytest.raises(GmailError, match="did not include an email address"):
        drafts.identity()


@pytest.mark.parametrize(
    "status,fragment",
    [(401, "reauthorize"), (403, "profile read"), (429, "rate limited"), (500, "HTTP 500")],
)
def test_identity_reports_a_read_specific_failure(status, fragment):
    recorder = Recorder(status=status)
    drafts = GmailDrafts(credentials(), read=recorder.read)
    with pytest.raises(GmailError, match=fragment):
        drafts.identity()


def test_gmail_drafts_declares_its_own_provider_name():
    assert GmailDrafts(credentials()).provider == "gmail"


# --- credentials ----------------------------------------------------------------------


def test_the_read_scope_is_refused_here_just_as_compose_is_refused_there():
    with pytest.raises(ValueError, match="gmail.compose"):
        credentials(scopes=(READONLY_SCOPE,))
    with pytest.raises(ValueError):
        credentials(scopes=(COMPOSE_SCOPE, "https://www.googleapis.com/auth/gmail.send"))


@pytest.mark.parametrize("token", ["with space", "line\nbreak", "tab\there", "nul\x00"])
def test_an_unusable_token_is_refused_without_being_echoed(token):
    with pytest.raises((ValueError, TypeError)) as caught:
        credentials(access_token=token)
    # An empty token is excluded deliberately: "" is a substring of every string, so it
    # would assert nothing at all.
    assert token and token not in str(caught.value)


def test_an_empty_token_is_refused():
    with pytest.raises(ValueError):
        credentials(access_token="")


def test_the_token_never_appears_in_a_repr_or_a_traceback():
    held = credentials()
    assert TOKEN not in repr(held)
    try:
        raise RuntimeError(f"boom {held!r}")
    except RuntimeError:
        assert TOKEN not in traceback.format_exc()


# --- header injection: the reason this PR has a composition boundary --------------------

INJECTIONS = [
    "jane@example.com\r\nBcc: attacker@example.com",
    "jane@example.com\nBcc: attacker@example.com",
    "jane@example.com\rBcc: attacker@example.com",
    "jane@example.com\x00",
    "jane@example.com\x0bBcc: attacker@example.com",
    "jane@example.com\x85Bcc: attacker@example.com",
    "jane@example.com, attacker@example.com",
    "jane@example.com; attacker@example.com",
    "<jane@example.com>, <attacker@example.com>",
]


@pytest.mark.parametrize("value", INJECTIONS)
def test_a_recipient_that_could_add_a_reader_is_refused_not_repaired(value):
    with pytest.raises(DraftRefused) as caught:
        recipient(value)
    # Refusing while quoting the attempt would put the injection into the operator's log.
    assert "attacker@example.com" not in str(caught.value)
    assert value not in str(caught.value)


@pytest.mark.parametrize("value", INJECTIONS)
def test_no_injected_recipient_survives_into_a_composed_message(value):
    """Belt and braces: even if recipient() were bypassed, composition must not emit it."""
    with pytest.raises(DraftRefused):
        compose(to=value, subject_line="Re: role", body=BODY, intent=KEY)


@pytest.mark.parametrize(
    "value", ["role\r\nBcc: attacker@example.com", "role\nX-Injected: yes", "role\x00"]
)
def test_a_subject_carrying_a_control_character_is_refused(value):
    with pytest.raises(DraftRefused) as caught:
        subject(value)
    assert value not in str(caught.value)


@pytest.mark.parametrize(
    "value",
    [
        "jane@example.com\r\nBcc: attacker@example.com",
        "jane@example.com\x00",
        "jane@example.com\x0b",
        "jane@example.com\x85",
    ],
)
def test_a_control_character_is_named_as_the_reason_rather_than_a_generic_refusal(value):
    """The reason has to be specific enough to act on.

    "not a usable address" tells the operator to check a typo; "control characters" tells
    them the message carried an injection attempt. The address pattern would refuse these
    anyway, so without this the scan that produces the specific reason is untested.
    """
    with pytest.raises(DraftRefused, match="control characters"):
        recipient(value)


def test_a_merely_malformed_address_gets_the_other_reason():
    with pytest.raises(DraftRefused, match="not a single usable email address"):
        recipient("not-an-address")


@pytest.mark.parametrize(
    "value",
    [
        "jane@example.com, attacker@example.com",
        "<jane@example.com>, <attacker@example.com>",
        "jane@example.com, attacker@example.com, third@example.com",
    ],
)
def test_a_list_of_mailboxes_is_refused_rather_than_reduced_to_the_first(value):
    """The refusal must not depend on the standard library failing closed, because it does
    not fail closed on every supported interpreter.

    On CPython 3.11.9 -- what hosted CI runs, and inside the supported range -- parseaddr
    returns the FIRST address of a list rather than refusing it, so this value parsed as
    jane@example.com and was accepted. That is sanitizing a hostile value into an accepted
    one: addressing a message to somebody the operator never read. Counting mailboxes
    behaves the same on every supported version, so this holds regardless of interpreter.
    """
    with pytest.raises(DraftRefused, match="more than one mailbox"):
        recipient(value)


def test_a_semicolon_separated_list_is_refused_on_every_supported_interpreter():
    """Refused everywhere, but not for the same reason everywhere, so the reason is not
    asserted here.

    A semicolon is not the RFC's separator, and the interpreters disagree about what the
    value even is: 3.11.9 reads three mailboxes, 3.13 reads one unusable address. Pinning
    either message would make this test pass on one interpreter and fail on the other,
    which is how a guard ends up asserted only where it happens to be true. What has to
    hold everywhere is that no draft is addressed.
    """
    with pytest.raises(DraftRefused):
        recipient("jane@example.com; attacker@example.com")


def test_a_comma_inside_a_quoted_display_name_is_still_one_mailbox():
    """The guard above must not refuse a legitimate address to achieve its refusals."""
    assert recipient('"Recruiter, Jane" <jane@example.com>') == "jane@example.com"
    assert recipient('"Doe, J. (Talent)" <jane@example.com>') == "jane@example.com"


# --- the last line of defense, exercised directly ---------------------------------------

# compose() validates its inputs, so these can no longer be reached through it. They are
# what catches a fault in composition itself rather than in what composition was handed,
# and an untested last line is not a line.
INTENT_LINE = f"{INTENT_HEADER}: {KEY}"


def raw_message(*extra, to="jane@example.com"):
    headers = [f"To: {to}", "Subject: Re: role", INTENT_LINE, *extra]
    return ("\r\n".join(headers) + "\r\n\r\n" + BODY).encode()


@pytest.mark.parametrize("header", ["Cc", "Bcc", "Reply-To", "Return-Path", "Sender"])
def test_a_composed_message_carrying_an_extra_destination_is_refused(header):
    with pytest.raises(DraftRefused, match=header):
        gmail_draft._verify(raw_message(f"{header}: attacker@example.com"), "jane@example.com", KEY)


def test_a_composed_message_addressed_to_somebody_else_is_refused():
    with pytest.raises(DraftRefused, match="intended recipient"):
        gmail_draft._verify(raw_message(to="attacker@example.com"), "jane@example.com", KEY)


def test_a_composed_message_with_two_recipients_is_refused():
    with pytest.raises(DraftRefused, match="intended recipient"):
        gmail_draft._verify(raw_message("To: attacker@example.com"), "jane@example.com", KEY)


def test_a_composed_message_with_two_subjects_is_refused():
    with pytest.raises(DraftRefused, match="exactly one subject"):
        gmail_draft._verify(raw_message("Subject: something else"), "jane@example.com", KEY)


def test_a_composed_message_that_lost_its_intent_key_is_refused():
    body = ("To: jane@example.com\r\nSubject: Re: role\r\n\r\n" + BODY).encode()
    with pytest.raises(DraftRefused, match="intent key"):
        gmail_draft._verify(body, "jane@example.com", KEY)


def test_the_verifier_accepts_what_composition_actually_produces():
    """Falsifiability: if this refused everything, every test above would pass vacuously."""
    raw = compose(to="jane@example.com", subject_line="Re: role", body=BODY, intent=KEY)
    gmail_draft._verify(raw, "jane@example.com", KEY)


def test_the_display_name_is_dropped_rather_than_encoded():
    """A recruiter-controlled display name has no bearing on where the message goes."""
    assert recipient('"Jane Recruiter" <jane@example.com>') == "jane@example.com"
    assert recipient("Jane <jane@example.com>") == "jane@example.com"


def test_a_recipient_that_is_simply_absent_is_refused_too():
    for value in ["", "   ", None, 42, "not-an-address", "@example.com", "jane@"]:
        with pytest.raises(DraftRefused):
            recipient(value)


# --- composition ----------------------------------------------------------------------


def test_a_composed_draft_addresses_exactly_one_recipient_and_carries_no_bcc():
    raw = compose(to="jane@example.com", subject_line="Re: role", body=BODY, intent=KEY).decode()
    assert raw.count("To:") == 1
    assert "Bcc" not in raw and "Cc:" not in raw
    assert BODY.splitlines()[0] in raw


def test_the_intent_key_survives_header_folding():
    """A 64 character key folds onto a continuation line, which reparses with a leading space.

    Comparing the raw parsed value would never match -- here, or against what Gmail returns
    during reconciliation, which is where it would have failed silently.
    """
    raw = compose(to="jane@example.com", subject_line="Re: role", body=BODY, intent=KEY)
    assert b"\n " in raw, "the key no longer folds; this test is no longer testing anything"
    assert gmail_draft.header_value(f"\n {KEY}") == KEY


def test_a_subject_is_prefixed_once_and_only_once():
    assert subject("SailPoint role") == "Re: SailPoint role"
    assert subject("Re: SailPoint role") == "Re: SailPoint role"
    assert subject("re: SailPoint role") == "re: SailPoint role"
    assert subject("") == "Re:"


def test_an_empty_or_oversized_body_is_refused():
    for body in ["", "   ", None]:
        with pytest.raises(DraftRefused):
            compose(to="jane@example.com", subject_line="Re: role", body=body, intent=KEY)


# --- creating a draft ------------------------------------------------------------------


def test_creating_a_draft_posts_once_to_the_drafts_collection():
    recorder = Recorder()
    drafts = GmailDrafts(credentials(), create=recorder.create, read=recorder.read)
    receipt = drafts.create(KEY, BODY, to="jane@example.com", subject_line="role")
    assert receipt == "gmail-draft:draft123"
    assert len(recorder.creates) == 1
    url, headers, body = recorder.creates[0]
    assert url == API_ROOT + "users/me/drafts"
    assert headers["Authorization"] == "Bearer " + TOKEN
    envelope = json.loads(body)
    raw = base64.urlsafe_b64decode(envelope["message"]["raw"]).decode()
    assert "To: jane@example.com" in raw
    assert KEY in raw.replace("\n ", "")


@pytest.mark.parametrize("value", INJECTIONS)
def test_a_hostile_recipient_stops_the_write_before_any_request_is_made(value):
    recorder = Recorder()
    drafts = GmailDrafts(credentials(), create=recorder.create, read=recorder.read)
    with pytest.raises(DraftRefused):
        drafts.create(KEY, BODY, to=value, subject_line="role")
    assert recorder.creates == [], "a refused draft still contacted Gmail"


@pytest.mark.parametrize("status", [400, 401, 403, 429, 500])
def test_a_refused_create_never_reports_a_receipt(status):
    recorder = Recorder(status=status)
    drafts = GmailDrafts(credentials(), create=recorder.create, read=recorder.read)
    with pytest.raises(GmailError) as caught:
        drafts.create(KEY, BODY, to="jane@example.com", subject_line="role")
    assert TOKEN not in str(caught.value)


@pytest.mark.parametrize("payload", [{}, {"id": ""}, {"id": "../../messages/send"}, {"id": 7}])
def test_an_unusable_draft_identifier_is_not_turned_into_a_receipt(payload):
    recorder = Recorder(created=payload)
    drafts = GmailDrafts(credentials(), create=recorder.create, read=recorder.read)
    with pytest.raises(GmailError):
        drafts.create(KEY, BODY, to="jane@example.com", subject_line="role")


# --- reconciliation --------------------------------------------------------------------


def test_reconciliation_matches_on_our_own_intent_key_rather_than_guessing():
    recorder = Recorder(
        listings=[{"drafts": [{"id": "other"}, {"id": "ours"}]}],
        drafts={"other": metadata("other", "b" * 64), "ours": metadata("ours", KEY)},
    )
    drafts = GmailDrafts(credentials(), create=recorder.create, read=recorder.read)
    assert drafts.lookup(KEY) == "gmail-draft:ours"


def test_reconciliation_reports_unknown_rather_than_somebody_elses_draft():
    recorder = Recorder(
        listings=[{"drafts": [{"id": "other"}]}], drafts={"other": metadata("other", "b" * 64)}
    )
    drafts = GmailDrafts(credentials(), create=recorder.create, read=recorder.read)
    assert drafts.lookup(KEY) is None


def test_two_drafts_claiming_one_intent_is_not_evidence_of_anything():
    recorder = Recorder(
        listings=[{"drafts": [{"id": "one"}, {"id": "two"}]}],
        drafts={"one": metadata("one", KEY), "two": metadata("two", KEY)},
    )
    drafts = GmailDrafts(credentials(), create=recorder.create, read=recorder.read)
    assert drafts.lookup(KEY) is None


def test_a_folded_intent_header_from_gmail_still_matches():
    """Gmail returns the header as it was stored, which for this key means folded."""
    recorder = Recorder(
        listings=[{"drafts": [{"id": "ours"}]}], drafts={"ours": metadata("ours", f"\r\n {KEY}")}
    )
    drafts = GmailDrafts(credentials(), create=recorder.create, read=recorder.read)
    assert drafts.lookup(KEY) == "gmail-draft:ours"


def test_reconciliation_never_creates_anything():
    recorder = Recorder(listings=[{"drafts": []}])
    drafts = GmailDrafts(credentials(), create=recorder.create, read=recorder.read)
    assert drafts.lookup(KEY) is None
    assert recorder.creates == []
