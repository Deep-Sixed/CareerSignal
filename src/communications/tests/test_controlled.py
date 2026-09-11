"""The in-memory provider's own identity: fixed, and never a mailbox to be wrong about."""

from communications.controlled import ControlledDrafts


def test_the_provider_and_namespace_are_fixed():
    drafts = ControlledDrafts()
    assert drafts.provider == "controlled"
    assert drafts.namespace == "controlled"


def test_identity_answers_without_contacting_anything():
    drafts = ControlledDrafts()
    assert drafts.identity() == "controlled"
    assert drafts.calls == 0


def test_every_instance_declares_the_same_identity():
    """Nothing about construction can make one instance a different destination."""
    assert ControlledDrafts().identity() == ControlledDrafts().identity()
