"""Tests for the write-back visibility guard (R-12, plan D10)."""

from jbi.visibility import can_write_comment, is_bug_restricted


def test_public_bug_is_not_restricted(bug_factory):
    assert is_bug_restricted(bug_factory(is_private=False, groups=[])) is False
    assert can_write_comment(bug_factory(is_private=False, groups=[])) is True


def test_private_bug_is_restricted(bug_factory):
    bug = bug_factory(is_private=True, groups=[])

    assert is_bug_restricted(bug) is True
    assert can_write_comment(bug) is False


def test_group_restricted_bug_is_restricted(bug_factory):
    """`groups` is how BMO marks security/embargoed bugs. JBI cannot
    reproduce those restrictions on a comment, so it stays out."""
    bug = bug_factory(is_private=False, groups=["mozilla-employee-confidential"])

    assert is_bug_restricted(bug) is True
    assert can_write_comment(bug) is False


def test_restricted_bug_is_logged(bug_factory, capturelogs):
    import logging

    bug = bug_factory(is_private=False, groups=["core-security"])

    with capturelogs.for_logger("jbi.visibility").at_level(logging.INFO):
        can_write_comment(bug)

    assert any("restricted" in record.message for record in capturelogs.records)
