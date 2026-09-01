from __future__ import annotations

import subprocess

import pytest

from tests.e2e.core.grpc_client import GRPCClient
from tests.e2e.core.helpers import assert_grpc_rejected
from tests.e2e.core.keycloak_admin import wait_for_project_not_in_keycloak


def test_project_full_lifecycle(
    jwt_grpc_tenant1_admin: GRPCClient,
    jwt_grpc_tenant1: GRPCClient,
    jwt_grpc_tenant2: GRPCClient,
    project_lifecycle_resources: dict[str, str | bool],
) -> None:
    """
    Full E2E test scenario for Projects:

    1. Verify parent project exists and is accessible
    2. Verify project exists in Keycloak
    3. Verify ProjectMembership grants access to tenant1_user
    4. Verify child project exists and parent-child relationship
    5. Verify tenant isolation (tenant2_user cannot access)
    6. Verify deletion constraints (parent with children)
    7. Delete child project, verify removal
    8. Delete parent project, verify removal

    Resources are created and cleaned up via project_lifecycle_resources fixture.
    """
    # Extract resource IDs from fixture
    parent_project_id = project_lifecycle_resources["parent_project_id"]
    parent_project_name = project_lifecycle_resources["parent_project_name"]
    child_project_id = project_lifecycle_resources["child_project_id"]
    child_project_name = project_lifecycle_resources["child_project_name"]
    membership_id = project_lifecycle_resources["membership_id"]
    keycloak_url = project_lifecycle_resources["keycloak_url"]
    admin_token = project_lifecycle_resources["admin_token"]
    org_id = project_lifecycle_resources["org_id"]
    skip_keycloak_sync_checks = project_lifecycle_resources["skip_keycloak_sync_checks"]

    # 1. Verify parent project exists
    assert parent_project_id != "", "Parent project ID should not be empty"
    assert parent_project_id in jwt_grpc_tenant1_admin.list_project_ids()

    # 2. Verify parent project exists in Keycloak (already checked in fixture, but verify again)
    # This verification is handled by the fixture's wait_for_project_in_keycloak call

    # 3. Get the project as tenant1_user (has access via membership)
    project_response = jwt_grpc_tenant1.get_project(project_id=parent_project_id)
    assert project_response["object"]["id"] == parent_project_id
    assert project_response["object"]["metadata"]["name"] == parent_project_name

    # 4. Verify child project and parent-child relationship
    assert child_project_id != "", "Child project ID should not be empty"
    assert child_project_id in jwt_grpc_tenant1_admin.list_project_ids()

    parent_check = jwt_grpc_tenant1.get_project(project_id=parent_project_id)
    assert parent_check["object"]["id"] == parent_project_id

    child_check = jwt_grpc_tenant1.get_project(project_id=child_project_id)
    assert child_check["object"]["id"] == child_project_id
    assert child_check["object"]["spec"]["parent_project"]["id"] == parent_project_id

    # Verify child project exists in Keycloak (already checked in fixture)

    # 5. Attempt to get projects as tenant2_user (different tenant, should fail)
    with pytest.raises(subprocess.CalledProcessError) as exc_info:
        jwt_grpc_tenant2.get_project(project_id=parent_project_id)
    assert_grpc_rejected(exc_info, "NotFound")

    with pytest.raises(subprocess.CalledProcessError) as exc_info:
        jwt_grpc_tenant2.get_project(project_id=child_project_id)
    assert_grpc_rejected(exc_info, "NotFound")

    # 6. Attempt to delete parent project (should fail due to child project)
    with pytest.raises(subprocess.CalledProcessError) as exc_info:
        jwt_grpc_tenant1_admin.delete_project(project_id=parent_project_id)
    assert_grpc_rejected(exc_info, "FailedPrecondition")
    # Verify error message mentions child projects
    error_output = (exc_info.value.stderr or "") + (exc_info.value.stdout or "")
    assert "child" in error_output.lower(), "Error message should mention child projects"

    # Verify parent still exists
    assert parent_project_id in jwt_grpc_tenant1_admin.list_project_ids()

    # 7. Delete the child project, verify removal from gRPC and Keycloak
    jwt_grpc_tenant1_admin.delete_project(project_id=child_project_id)

    # Verify child removed from gRPC
    assert child_project_id not in jwt_grpc_tenant1_admin.list_project_ids()

    # Verify child removed from Keycloak with polling
    if not skip_keycloak_sync_checks:
        wait_for_project_not_in_keycloak(
            keycloak_url=keycloak_url, admin_token=admin_token, org_id=org_id, project_name=child_project_name
        )

    # 8. Delete the parent project, verify removal from gRPC and Keycloak
    jwt_grpc_tenant1_admin.delete_project(project_id=parent_project_id)

    # Verify parent removed from gRPC
    assert parent_project_id not in jwt_grpc_tenant1_admin.list_project_ids()

    # Verify parent removed from Keycloak with polling
    if not skip_keycloak_sync_checks:
        wait_for_project_not_in_keycloak(
            keycloak_url=keycloak_url, admin_token=admin_token, org_id=org_id, project_name=parent_project_name
        )

    # Delete the ProjectMembership
    jwt_grpc_tenant1_admin.delete_project_membership(membership_id=membership_id)

    # Note: Cleanup of all resources happens automatically in the fixture's finally block
