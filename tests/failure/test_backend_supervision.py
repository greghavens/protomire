"""Process observation is tri-state; absence is never inferred from an error."""
from unittest.mock import patch

from modelbuilder.backend.common import process_group_state, termination_confirmed


def test_unreachable_process_group_is_unknown():
    with patch("os.killpg", side_effect=PermissionError("unobservable")):
        assert process_group_state(1234567) is None


def test_missing_group_is_positively_absent():
    with patch("os.killpg", side_effect=ProcessLookupError("absent")):
        assert process_group_state(1234567) is False


def test_pid_without_birth_identity_is_not_termination_evidence():
    assert not termination_confirmed({"worker_pid": 1234567, "worker_reaped": True, "process_group_terminated": True})


def test_explicit_wait_and_group_proof_is_terminal():
    assert termination_confirmed({"worker_pid": 1234567, "worker_identity": "birth-marker",
                                  "worker_reaped": True, "process_group_terminated": True})


def test_unknown_group_proof_is_not_terminal():
    assert not termination_confirmed({"worker_pid": 1234567, "worker_identity": "birth-marker",
                                      "worker_reaped": True, "process_group_terminated": None})
