from __future__ import annotations

import pytest

from tests.e2e.core.grpc_client import GRPCClient
from tests.e2e.core.helpers import unique_name, wait_for_cr, wait_for_deletion, wait_for_grpc_removal
from tests.e2e.core.k8s_client import K8sClient
from tests.e2e.core.metering import MeteringCollector
from tests.e2e.core.osac_cli import OsacCLI
from tests.e2e.core.runner import poll_until

pytestmark = pytest.mark.regression

_TERMINAL_JOB_STATES: tuple[str, ...] = ("Canceled", "Failed", "Succeeded")


def _get_deprovision_status(k8s: K8sClient, *, name: str) -> str:
    if not k8s.is_present(resource="computeinstance", name=name):
        return "cr_deleted"
    return k8s.get_compute_instance_latest_job_state(name=name, job_type="deprovision", checked=False)


def _wait_for_provision_termination(k8s: K8sClient, *, name: str) -> None:
    """Wait for the active provision AAP job to reach a terminal state.

    When a VM is deleted while still provisioning, the operator cancels the
    in-flight AAP provision job before starting deprovision.  Without this
    wait, `wait_for_deletion` races against the full cancel-then-deprovision
    pipeline and can time out.
    """
    poll_until(
        fn=lambda: k8s.get_compute_instance_latest_job_state(name=name, job_type="provision", checked=False),
        until=lambda v: v in (*_TERMINAL_JOB_STATES, ""),
        retries=60,
        delay=2,
        description=f"provision termination for {name}",
    )


def _wait_for_deprovision_completion(k8s: K8sClient, *, name: str) -> None:
    """Wait for the deprovision AAP job to complete or the CR to be removed."""
    result: str = poll_until(
        fn=lambda: _get_deprovision_status(k8s, name=name),
        until=lambda v: v in (*_TERMINAL_JOB_STATES, "cr_deleted"),
        retries=120,
        delay=5,
        description=f"deprovision completion for {name}",
    )
    assert result != "Failed", f"Deprovision job failed for {name}"


@pytest.mark.metering
def test_short_lived_vm_metering(
    cli: OsacCLI,
    grpc: GRPCClient,
    k8s_hub_client: K8sClient,
    vm_template: str,
    default_network_attachment: dict[str, object],
    metering: MeteringCollector,
) -> None:
    """Verify metering captures events for a VM created and deleted within 30s (CAP-4).

    A resource existing for 30 seconds must appear in usage data. This validates
    sub-minute billing granularity by creating a VM and immediately deleting it
    without waiting for it to reach Running.

    The deleted-event timeout is extended to 10 minutes because tearing down a VM
    that is still actively provisioning can take significantly longer than deleting
    one that has already reached a steady state.  The test explicitly waits for
    provision cancellation and deprovision completion to avoid racing the full
    cancel-then-deprovision pipeline when the operator has not yet finished
    provisioning.
    """
    uuid: str = cli.create_compute_instance(
        name=unique_name("e2e-ci"), template=vm_template, network_attachments=[default_network_attachment]
    )
    metering.expect("osac.resource.created.v1", resource_id=uuid)

    ci_name: str = wait_for_cr(k8s=k8s_hub_client, uuid=uuid)

    cli.delete_compute_instance(uuid=uuid)
    metering.expect("osac.resource.deleted.v1", resource_id=uuid, timeout=600)

    _wait_for_provision_termination(k8s_hub_client, name=ci_name)
    _wait_for_deprovision_completion(k8s_hub_client, name=ci_name)

    wait_for_deletion(k8s=k8s_hub_client, name=ci_name)
    wait_for_grpc_removal(grpc=grpc, uuid=uuid)
