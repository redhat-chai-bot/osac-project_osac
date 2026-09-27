from __future__ import annotations

import contextlib
import logging
import subprocess
from uuid import uuid4

import pytest

from tests.e2e.catalog.conftest import unique_name
from tests.e2e.core.grpc_client import PUBLIC_API, GRPCClient
from tests.e2e.core.helpers import (
    wait_for_cluster_deleting,
    wait_for_cluster_deletion,
    wait_for_cluster_grpc_deleting_or_archived,
    wait_for_cluster_grpc_removal,
    wait_for_cluster_order_cr,
    wait_for_cluster_progressing,
    wait_for_security_group_cr,
    wait_for_security_group_deletion,
    wait_for_security_group_ready,
    wait_for_subnet_cr,
    wait_for_subnet_deletion,
    wait_for_subnet_ready,
    wait_for_virtual_network_cr,
    wait_for_virtual_network_deletion,
    wait_for_virtual_network_ready,
)
from tests.e2e.core.k8s_client import K8sClient
from tests.e2e.core.osac_cli import OsacCLI

logger = logging.getLogger(__name__)

pytestmark = pytest.mark.sanity


def _create_cluster_grpc(
    grpc: GRPCClient, *, name: str, template: str, subnet_id: str, security_group_ids: list[str] | None = None
) -> str:
    """Create a cluster via gRPC with a ClusterNetworkAttachment."""
    network_attachment: dict = {"subnet": {"id": subnet_id}}
    if security_group_ids:
        network_attachment["security_groups"] = [{"id": sg_id} for sg_id in security_group_ids]

    response = grpc.call(
        service=f"{PUBLIC_API}.Clusters/Create",
        data={
            "object": {
                "metadata": {"name": name},
                "spec": {"template": {"name": template, "shared": True}, "network_attachment": network_attachment},
            }
        },
    )
    return response["object"]["id"]


def _create_cluster_grpc_unchecked(
    grpc: GRPCClient, *, name: str, template: str, subnet_id: str, security_group_ids: list[str] | None = None
) -> tuple[str, int]:
    """Create a cluster via gRPC (unchecked) with a ClusterNetworkAttachment."""
    network_attachment: dict = {"subnet": {"id": subnet_id}}
    if security_group_ids:
        network_attachment["security_groups"] = [{"id": sg_id} for sg_id in security_group_ids]

    return grpc.call_unchecked(
        service=f"{PUBLIC_API}.Clusters/Create",
        data={
            "object": {
                "metadata": {"name": name},
                "spec": {"template": {"name": template, "shared": True}, "network_attachment": network_attachment},
            }
        },
    )


def _cleanup_cluster(cli: OsacCLI, grpc: GRPCClient, k8s: K8sClient, *, uuid: str, co_name: str | None) -> None:
    """Best-effort cluster teardown."""
    with contextlib.suppress(subprocess.SubprocessError):
        cli.delete_cluster(uuid=uuid)
    if co_name is not None:
        with contextlib.suppress(TimeoutError):
            wait_for_cluster_deletion(k8s=k8s, name=co_name)
    with contextlib.suppress(TimeoutError):
        wait_for_cluster_grpc_removal(grpc=grpc, uuid=uuid)


def _cleanup_networking(
    grpc: GRPCClient,
    k8s: K8sClient,
    *,
    sg_ids: list[str],
    sg_cr_names: list[str],
    subnet_id: str | None,
    subnet_cr_name: str | None,
    vn_id: str | None,
    vn_cr_name: str | None,
    vn_id_b: str | None = None,
    vn_cr_name_b: str | None = None,
    sg_ids_b: list[str] | None = None,
    sg_cr_names_b: list[str] | None = None,
) -> None:
    """Best-effort networking teardown in dependency order."""
    for sg_id, sg_cr in zip(sg_ids_b or [], sg_cr_names_b or [], strict=False):
        with contextlib.suppress(subprocess.SubprocessError):
            grpc.delete_security_group(sg_id=sg_id)
        with contextlib.suppress(TimeoutError):
            wait_for_security_group_deletion(k8s=k8s, name=sg_cr)

    for sg_id, sg_cr in zip(sg_ids, sg_cr_names, strict=False):
        with contextlib.suppress(subprocess.SubprocessError):
            grpc.delete_security_group(sg_id=sg_id)
        with contextlib.suppress(TimeoutError):
            wait_for_security_group_deletion(k8s=k8s, name=sg_cr)

    if subnet_id is not None:
        with contextlib.suppress(subprocess.SubprocessError):
            grpc.delete_subnet(subnet_id=subnet_id)
        if subnet_cr_name is not None:
            with contextlib.suppress(TimeoutError):
                wait_for_subnet_deletion(k8s=k8s, name=subnet_cr_name)

    if vn_id is not None:
        with contextlib.suppress(subprocess.SubprocessError):
            grpc.delete_virtual_network(vn_id=vn_id)
        if vn_cr_name is not None:
            with contextlib.suppress(TimeoutError):
                wait_for_virtual_network_deletion(k8s=k8s, name=vn_cr_name)

    if vn_id_b is not None:
        with contextlib.suppress(subprocess.SubprocessError):
            grpc.delete_virtual_network(vn_id=vn_id_b)
        if vn_cr_name_b is not None:
            with contextlib.suppress(TimeoutError):
                wait_for_virtual_network_deletion(k8s=k8s, name=vn_cr_name_b)


def test_cluster_create_with_network_attachment(
    cli: OsacCLI, grpc: GRPCClient, k8s_hub_client: K8sClient, cluster_template: str
) -> None:
    """Happy path: create a cluster with a valid ClusterNetworkAttachment and verify
    the network_attachment fields are persisted and retrievable."""
    tag = uuid4().hex[:8]
    vn_id: str | None = None
    vn_cr_name: str | None = None
    subnet_id: str | None = None
    subnet_cr_name: str | None = None
    sg_id: str | None = None
    sg_cr_name: str | None = None
    cluster_uuid: str | None = None
    co_name: str | None = None

    try:
        # Set up networking prerequisites
        vn_id = grpc.create_virtual_network(name=f"e2e-cna-vn-{tag}", ipv4_cidr="10.220.0.0/16")
        vn_cr_name = wait_for_virtual_network_cr(k8s=k8s_hub_client, uuid=vn_id)
        wait_for_virtual_network_ready(k8s=k8s_hub_client, name=vn_cr_name)

        subnet_id = grpc.create_subnet(name=f"e2e-cna-sub-{tag}", virtual_network=vn_id, ipv4_cidr="10.220.1.0/24")
        subnet_cr_name = wait_for_subnet_cr(k8s=k8s_hub_client, uuid=subnet_id)
        wait_for_subnet_ready(k8s=k8s_hub_client, name=subnet_cr_name)

        sg_id = grpc.create_security_group(name=f"e2e-cna-sg-{tag}", virtual_network=vn_id)
        sg_cr_name = wait_for_security_group_cr(k8s=k8s_hub_client, uuid=sg_id)
        wait_for_security_group_ready(k8s=k8s_hub_client, name=sg_cr_name)

        # Create cluster with network_attachment
        cluster_name = unique_name("e2e-cna-cluster")
        cluster_uuid = _create_cluster_grpc(
            grpc, name=cluster_name, template=cluster_template, subnet_id=subnet_id, security_group_ids=[sg_id]
        )

        co_name = wait_for_cluster_order_cr(k8s=k8s_hub_client, uuid=cluster_uuid)
        assert cluster_uuid in grpc.list_cluster_ids()

        wait_for_cluster_progressing(k8s=k8s_hub_client, name=co_name)

        # Verify network_attachment is persisted in the cluster spec
        cluster = grpc.get_cluster(cluster_id=cluster_uuid)
        spec = cluster["object"]["spec"]
        na = spec.get("network_attachment", spec.get("networkAttachment", {}))
        na_subnet = na.get("subnet", {})
        na_sgs = na.get("security_groups", na.get("securityGroups", []))
        assert na_subnet.get("id") == subnet_id, f"Expected subnet id {subnet_id}, got {na_subnet}"
        assert len(na_sgs) == 1, f"Expected 1 security group, got {len(na_sgs)}"
        assert na_sgs[0].get("id") == sg_id, f"Expected security group id {sg_id}, got {na_sgs[0]}"

        # Teardown cluster
        cli.delete_cluster(uuid=cluster_uuid)
        wait_for_cluster_deleting(k8s=k8s_hub_client, name=co_name)
        wait_for_cluster_grpc_deleting_or_archived(grpc=grpc, uuid=cluster_uuid)
        wait_for_cluster_deletion(k8s=k8s_hub_client, name=co_name)
        wait_for_cluster_grpc_removal(grpc=grpc, uuid=cluster_uuid)
        cluster_uuid = None
    finally:
        if cluster_uuid is not None:
            _cleanup_cluster(cli, grpc, k8s_hub_client, uuid=cluster_uuid, co_name=co_name)
        _cleanup_networking(
            grpc,
            k8s_hub_client,
            sg_ids=[sg_id] if sg_id else [],
            sg_cr_names=[sg_cr_name] if sg_cr_name else [],
            subnet_id=subnet_id,
            subnet_cr_name=subnet_cr_name,
            vn_id=vn_id,
            vn_cr_name=vn_cr_name,
        )


def test_cluster_create_rejected_subnet_not_ready(
    grpc: GRPCClient, k8s_hub_client: K8sClient, cluster_template: str
) -> None:
    """Creating a cluster with a subnet that is not yet READY must be rejected."""
    tag = uuid4().hex[:8]
    vn_id: str | None = None
    vn_cr_name: str | None = None
    subnet_id: str | None = None

    try:
        vn_id = grpc.create_virtual_network(name=f"e2e-cna-notready-vn-{tag}", ipv4_cidr="10.221.0.0/16")
        vn_cr_name = wait_for_virtual_network_cr(k8s=k8s_hub_client, uuid=vn_id)
        wait_for_virtual_network_ready(k8s=k8s_hub_client, name=vn_cr_name)

        # Create subnet but do NOT wait for it to become READY
        subnet_id = grpc.create_subnet(
            name=f"e2e-cna-notready-sub-{tag}", virtual_network=vn_id, ipv4_cidr="10.221.1.0/24"
        )
        # Immediately try cluster creation before the subnet reaches READY
        output, rc = _create_cluster_grpc_unchecked(
            grpc, name=unique_name("e2e-cna-notready"), template=cluster_template, subnet_id=subnet_id
        )
        assert rc != 0, f"Expected cluster creation to be rejected for non-ready subnet, got: {output}"
        assert "FailedPrecondition" in output or "not ready" in output.lower() or "READY" in output, (
            f"Expected rejection for subnet not in READY state, got: {output}"
        )
    finally:
        subnet_cr_name_resolved = None
        if subnet_id is not None:
            with contextlib.suppress(TimeoutError):
                subnet_cr_name_resolved = wait_for_subnet_cr(k8s=k8s_hub_client, uuid=subnet_id)
        _cleanup_networking(
            grpc,
            k8s_hub_client,
            sg_ids=[],
            sg_cr_names=[],
            subnet_id=subnet_id,
            subnet_cr_name=subnet_cr_name_resolved,
            vn_id=vn_id,
            vn_cr_name=vn_cr_name,
        )


def test_cluster_create_rejected_nonexistent_subnet(grpc: GRPCClient, cluster_template: str) -> None:
    """Creating a cluster referencing a nonexistent subnet must be rejected."""
    fake_subnet_id = f"nonexistent-subnet-{uuid4().hex[:8]}"

    with pytest.raises(subprocess.CalledProcessError) as exc_info:
        _create_cluster_grpc(
            grpc, name=unique_name("e2e-cna-nosub"), template=cluster_template, subnet_id=fake_subnet_id
        )
    combined = (exc_info.value.stderr or "") + (exc_info.value.stdout or "")
    assert "NotFound" in combined or "not found" in combined.lower() or "InvalidArgument" in combined, (
        f"Expected NotFound or InvalidArgument for nonexistent subnet, got: {combined.strip()}"
    )


def test_cluster_create_rejected_cross_vn_security_group(
    grpc: GRPCClient, k8s_hub_client: K8sClient, cluster_template: str
) -> None:
    """A security group from a different virtual network than the subnet must be rejected."""
    tag = uuid4().hex[:8]
    vn_a_id: str | None = None
    vn_a_cr_name: str | None = None
    vn_b_id: str | None = None
    vn_b_cr_name: str | None = None
    subnet_id: str | None = None
    subnet_cr_name: str | None = None
    sg_b_id: str | None = None
    sg_b_cr_name: str | None = None

    try:
        # VirtualNetwork A with a subnet
        vn_a_id = grpc.create_virtual_network(name=f"e2e-cna-vna-{tag}", ipv4_cidr="10.222.0.0/16")
        vn_a_cr_name = wait_for_virtual_network_cr(k8s=k8s_hub_client, uuid=vn_a_id)
        wait_for_virtual_network_ready(k8s=k8s_hub_client, name=vn_a_cr_name)

        subnet_id = grpc.create_subnet(
            name=f"e2e-cna-crossvn-sub-{tag}", virtual_network=vn_a_id, ipv4_cidr="10.222.1.0/24"
        )
        subnet_cr_name = wait_for_subnet_cr(k8s=k8s_hub_client, uuid=subnet_id)
        wait_for_subnet_ready(k8s=k8s_hub_client, name=subnet_cr_name)

        # VirtualNetwork B with a security group
        vn_b_id = grpc.create_virtual_network(name=f"e2e-cna-vnb-{tag}", ipv4_cidr="10.223.0.0/16")
        vn_b_cr_name = wait_for_virtual_network_cr(k8s=k8s_hub_client, uuid=vn_b_id)
        wait_for_virtual_network_ready(k8s=k8s_hub_client, name=vn_b_cr_name)

        sg_b_id = grpc.create_security_group(name=f"e2e-cna-crossvn-sg-{tag}", virtual_network=vn_b_id)
        sg_b_cr_name = wait_for_security_group_cr(k8s=k8s_hub_client, uuid=sg_b_id)
        wait_for_security_group_ready(k8s=k8s_hub_client, name=sg_b_cr_name)

        # Attempt cluster with subnet from VN-A and security group from VN-B
        output, rc = _create_cluster_grpc_unchecked(
            grpc,
            name=unique_name("e2e-cna-crossvn"),
            template=cluster_template,
            subnet_id=subnet_id,
            security_group_ids=[sg_b_id],
        )
        assert rc != 0, f"Expected cluster creation to be rejected for cross-VN security group, got: {output}"
        assert "InvalidArgument" in output or "FailedPrecondition" in output or "virtual network" in output.lower(), (
            f"Expected rejection for cross-VN security group, got: {output}"
        )
    finally:
        _cleanup_networking(
            grpc,
            k8s_hub_client,
            sg_ids=[],
            sg_cr_names=[],
            subnet_id=subnet_id,
            subnet_cr_name=subnet_cr_name,
            vn_id=vn_a_id,
            vn_cr_name=vn_a_cr_name,
            vn_id_b=vn_b_id,
            vn_cr_name_b=vn_b_cr_name,
            sg_ids_b=[sg_b_id] if sg_b_id else [],
            sg_cr_names_b=[sg_b_cr_name] if sg_b_cr_name else [],
        )


def test_cluster_network_attachment_subnet_immutable(
    cli: OsacCLI, grpc: GRPCClient, k8s_hub_client: K8sClient, cluster_template: str
) -> None:
    """After cluster creation, modifying network_attachment.subnet must be rejected."""
    tag = uuid4().hex[:8]
    vn_id: str | None = None
    vn_cr_name: str | None = None
    subnet_id: str | None = None
    subnet_cr_name: str | None = None
    subnet_b_id: str | None = None
    subnet_b_cr_name: str | None = None
    cluster_uuid: str | None = None
    co_name: str | None = None

    try:
        # Set up networking prerequisites
        vn_id = grpc.create_virtual_network(name=f"e2e-cna-immut-vn-{tag}", ipv4_cidr="10.224.0.0/16")
        vn_cr_name = wait_for_virtual_network_cr(k8s=k8s_hub_client, uuid=vn_id)
        wait_for_virtual_network_ready(k8s=k8s_hub_client, name=vn_cr_name)

        subnet_id = grpc.create_subnet(
            name=f"e2e-cna-immut-sub-{tag}", virtual_network=vn_id, ipv4_cidr="10.224.1.0/24"
        )
        subnet_cr_name = wait_for_subnet_cr(k8s=k8s_hub_client, uuid=subnet_id)
        wait_for_subnet_ready(k8s=k8s_hub_client, name=subnet_cr_name)

        # Create a second subnet to use for the immutability update attempt
        subnet_b_id = grpc.create_subnet(
            name=f"e2e-cna-immut-sub2-{tag}", virtual_network=vn_id, ipv4_cidr="10.224.2.0/24"
        )
        subnet_b_cr_name = wait_for_subnet_cr(k8s=k8s_hub_client, uuid=subnet_b_id)
        wait_for_subnet_ready(k8s=k8s_hub_client, name=subnet_b_cr_name)

        # Create cluster with the first subnet
        cluster_name = unique_name("e2e-cna-immut")
        cluster_uuid = _create_cluster_grpc(grpc, name=cluster_name, template=cluster_template, subnet_id=subnet_id)

        co_name = wait_for_cluster_order_cr(k8s=k8s_hub_client, uuid=cluster_uuid)
        wait_for_cluster_progressing(k8s=k8s_hub_client, name=co_name)

        # Attempt to update network_attachment.subnet to a different subnet
        output, rc = grpc.call_unchecked(
            service=f"{PUBLIC_API}.Clusters/Update",
            data={
                "object": {"id": cluster_uuid, "spec": {"network_attachment": {"subnet": {"id": subnet_b_id}}}},
                "updateMask": {"paths": ["spec.network_attachment"]},
            },
        )
        assert rc != 0, f"Expected update to be rejected for immutable subnet, got: {output}"
        assert "InvalidArgument" in output or "immutable" in output.lower() or "FailedPrecondition" in output, (
            f"Expected rejection for immutable network_attachment.subnet, got: {output}"
        )

        # Teardown cluster
        cli.delete_cluster(uuid=cluster_uuid)
        wait_for_cluster_deleting(k8s=k8s_hub_client, name=co_name)
        wait_for_cluster_grpc_deleting_or_archived(grpc=grpc, uuid=cluster_uuid)
        wait_for_cluster_deletion(k8s=k8s_hub_client, name=co_name)
        wait_for_cluster_grpc_removal(grpc=grpc, uuid=cluster_uuid)
        cluster_uuid = None
    finally:
        if cluster_uuid is not None:
            _cleanup_cluster(cli, grpc, k8s_hub_client, uuid=cluster_uuid, co_name=co_name)
        # Clean up both subnets
        if subnet_b_id is not None:
            with contextlib.suppress(subprocess.SubprocessError):
                grpc.delete_subnet(subnet_id=subnet_b_id)
            if subnet_b_cr_name is not None:
                with contextlib.suppress(TimeoutError):
                    wait_for_subnet_deletion(k8s=k8s_hub_client, name=subnet_b_cr_name)
        _cleanup_networking(
            grpc,
            k8s_hub_client,
            sg_ids=[],
            sg_cr_names=[],
            subnet_id=subnet_id,
            subnet_cr_name=subnet_cr_name,
            vn_id=vn_id,
            vn_cr_name=vn_cr_name,
        )
