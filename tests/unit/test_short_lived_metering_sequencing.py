"""Regression test for short-lived VM metering cleanup sequencing.

Verifies that test_short_lived_vm_metering waits for provision
termination and deprovision completion before polling for CR deletion,
preventing a timeout race when deleting a VM that is still provisioning.

Root cause: without explicit provision-termination and deprovision-completion
waits, the CR-deletion poll can start while the operator is still canceling
the AAP provision job, exhausting its 600-second budget before the operator
has a chance to remove the CR.

See: GitHub Actions run 38398721678 (E2E VMaaS Full Install, main@cfd490f).
"""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from tests.e2e.core import runner
from tests.e2e.core.k8s_client import K8sClient
from tests.e2e.vmaas.regression.test_compute_instance_short_lived_metering import (
    _wait_for_deprovision_completion,
    _wait_for_provision_termination,
)


class TestProvisionTerminationWait:
    """_wait_for_provision_termination polls until the provision job reaches a terminal state."""

    def test_returns_when_provision_canceled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(runner.time, "sleep", lambda _: None)
        k8s = Mock(spec=K8sClient)
        k8s.get_compute_instance_latest_job_state.side_effect = ["Running", "Running", "Canceled"]

        _wait_for_provision_termination(k8s, name="test-ci")

        assert k8s.get_compute_instance_latest_job_state.call_count == 3
        k8s.get_compute_instance_latest_job_state.assert_called_with(
            name="test-ci", job_type="provision", checked=False
        )

    def test_returns_immediately_when_no_provision_job(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(runner.time, "sleep", lambda _: None)
        k8s = Mock(spec=K8sClient)
        k8s.get_compute_instance_latest_job_state.return_value = ""

        _wait_for_provision_termination(k8s, name="test-ci")

        assert k8s.get_compute_instance_latest_job_state.call_count == 1

    def test_returns_when_provision_succeeded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(runner.time, "sleep", lambda _: None)
        k8s = Mock(spec=K8sClient)
        k8s.get_compute_instance_latest_job_state.return_value = "Succeeded"

        _wait_for_provision_termination(k8s, name="test-ci")

        assert k8s.get_compute_instance_latest_job_state.call_count == 1


class TestDeprovisionCompletionWait:
    """_wait_for_deprovision_completion polls until deprovision finishes or the CR is gone."""

    def test_returns_when_deprovision_succeeded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(runner.time, "sleep", lambda _: None)
        k8s = Mock(spec=K8sClient)
        k8s.is_present.return_value = True
        k8s.get_compute_instance_latest_job_state.side_effect = ["", "Running", "Succeeded"]

        _wait_for_deprovision_completion(k8s, name="test-ci")

        assert k8s.get_compute_instance_latest_job_state.call_count == 3

    def test_returns_when_cr_already_deleted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(runner.time, "sleep", lambda _: None)
        k8s = Mock(spec=K8sClient)
        k8s.is_present.return_value = False

        _wait_for_deprovision_completion(k8s, name="test-ci")

        k8s.is_present.assert_called_with(resource="computeinstance", name="test-ci")

    def test_fails_on_deprovision_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(runner.time, "sleep", lambda _: None)
        k8s = Mock(spec=K8sClient)
        k8s.is_present.return_value = True
        k8s.get_compute_instance_latest_job_state.return_value = "Failed"

        with pytest.raises(AssertionError, match="Deprovision job failed"):
            _wait_for_deprovision_completion(k8s, name="test-ci")

    def test_waits_through_pending_then_succeeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(runner.time, "sleep", lambda _: None)
        k8s = Mock(spec=K8sClient)
        k8s.is_present.return_value = True
        k8s.get_compute_instance_latest_job_state.side_effect = ["", "", "Running", "Running", "Canceled"]

        _wait_for_deprovision_completion(k8s, name="test-ci")

        assert k8s.get_compute_instance_latest_job_state.call_count == 5
