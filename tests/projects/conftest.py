from __future__ import annotations

import logging
import os
import re
import subprocess
from typing import Generator
from uuid import uuid4

import pytest

from tests.e2e.core.grpc_client import GRPCClient
from tests.e2e.core.keycloak_admin import (
    get_admin_token,
    wait_for_organization,
    wait_for_project_in_keycloak,
    wait_for_project_not_in_keycloak,
)

logger = logging.getLogger(__name__)


@pytest.fixture(scope="session")
def skip_keycloak_sync_checks() -> bool:
    """
    Check if Keycloak sync verification should be skipped.
    Set OSAC_SKIP_KEYCLOAK_SYNC=true to skip Keycloak sync checks.
    Useful when the project sync feature isn't deployed yet.
    """
    return os.getenv("OSAC_SKIP_KEYCLOAK_SYNC", "false").lower() in ("true", "1", "yes")


def unique_name(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:8]}"


def _delete_project_teardown(
    grpc: GRPCClient,
    *,
    project_id: str,
    project_name: str,
) -> None:
    """Delete a project, handling common errors gracefully."""
    try:
        grpc.delete_project(project_id=project_id)
    except subprocess.CalledProcessError as exc:
        combined = (exc.stderr or "") + (exc.stdout or "")
        if re.search(r"Code:\s*NotFound", combined):
            logger.warning("Project %s (%s) already deleted via API", project_name, project_id)
        else:
            logger.warning("Project %s (%s) teardown delete failed: %s", project_name, project_id, combined.strip())


def _delete_project_membership_teardown(
    grpc: GRPCClient,
    *,
    membership_id: str,
    membership_name: str,
) -> None:
    """Delete a project membership, handling common errors gracefully."""
    try:
        grpc.delete_project_membership(membership_id=membership_id)
    except subprocess.CalledProcessError as exc:
        combined = (exc.stderr or "") + (exc.stdout or "")
        if re.search(r"Code:\s*NotFound", combined):
            logger.warning("ProjectMembership %s (%s) already deleted via API", membership_name, membership_id)
        else:
            logger.warning(
                "ProjectMembership %s (%s) teardown delete failed: %s", membership_name, membership_id, combined.strip()
            )


@pytest.fixture(scope="function")
def project_lifecycle_resources(
    jwt_grpc_tenant1_admin: GRPCClient,
    keycloak_url: str,
    keycloak_admin_password: str,
    skip_keycloak_sync_checks: bool,
) -> Generator[dict[str, str | bool], None, None]:
    """
    Create parent project, child project, and project membership for lifecycle tests.
    Yields a dict with all IDs for use in the test.
    Cleanup happens in finally block regardless of test outcome.
    """
    tag = uuid4().hex[:8]
    parent_name = f"parent-project-{tag}"
    child_name = f"child-project-{tag}"
    membership_name = f"parent-project-viewer-{tag}"

    parent_project_id: str | None = None
    child_project_id: str | None = None
    membership_id: str | None = None

    admin_token: str | None = None
    org_id: str | None = None

    if not skip_keycloak_sync_checks:
        admin_token = get_admin_token(keycloak_url=keycloak_url, username="admin", password=keycloak_admin_password)
        org_id = wait_for_organization(keycloak_url=keycloak_url, admin_token=admin_token, org_name="tenant1")

    try:
        # Create parent project
        parent_project_id = jwt_grpc_tenant1_admin.create_project(name=parent_name)

        # Wait for Keycloak sync if enabled
        if not skip_keycloak_sync_checks:
            wait_for_project_in_keycloak(
                keycloak_url=keycloak_url, admin_token=admin_token, org_id=org_id, project_name=parent_name
            )

        # Create project membership
        # Note: ProjectMembership is scoped to the calling user's current project context.
        # Since jwt_grpc_tenant1_admin is authenticated within tenant1's organization,
        # this membership binds tenant1-user to the parent project created above.
        membership_id = jwt_grpc_tenant1_admin.create_project_membership(
            name=membership_name, user_names=["tenant1-user"], role="PROJECT_MEMBERSHIP_ROLE_VIEWER"
        )

        # Create child project
        child_project_id = jwt_grpc_tenant1_admin.create_project(name=child_name, parent_project_id=parent_project_id)

        # Wait for Keycloak sync if enabled
        if not skip_keycloak_sync_checks:
            wait_for_project_in_keycloak(
                keycloak_url=keycloak_url, admin_token=admin_token, org_id=org_id, project_name=child_name
            )

        yield {
            "parent_project_id": parent_project_id,
            "parent_project_name": parent_name,
            "child_project_id": child_project_id,
            "child_project_name": child_name,
            "membership_id": membership_id,
            "membership_name": membership_name,
            "keycloak_url": keycloak_url,
            "admin_token": admin_token or "",
            "org_id": org_id or "",
            "skip_keycloak_sync_checks": skip_keycloak_sync_checks,
        }
    except Exception:
        # If setup fails, cleanup any resources that were created
        logger.warning("Setup failed, cleaning up partial project lifecycle resources")
        if membership_id:
            _delete_project_membership_teardown(
                jwt_grpc_tenant1_admin, membership_id=membership_id, membership_name=membership_name
            )
        if child_project_id:
            _delete_project_teardown(jwt_grpc_tenant1_admin, project_id=child_project_id, project_name=child_name)
        if parent_project_id:
            _delete_project_teardown(
                jwt_grpc_tenant1_admin, project_id=parent_project_id, project_name=parent_name
            )
        raise
    finally:
        # Normal cleanup runs regardless of setup success/failure
        # Delete in reverse order: membership -> child -> parent
        # Note: The test body also performs explicit deletes to validate deletion constraints.
        # This cleanup ensures resources are removed even if the test fails partway through.
        if membership_id:
            _delete_project_membership_teardown(
                jwt_grpc_tenant1_admin, membership_id=membership_id, membership_name=membership_name
            )

        if child_project_id:
            _delete_project_teardown(jwt_grpc_tenant1_admin, project_id=child_project_id, project_name=child_name)
            # Verify child removed from Keycloak with polling
            if not skip_keycloak_sync_checks and admin_token and org_id:
                try:
                    wait_for_project_not_in_keycloak(
                        keycloak_url=keycloak_url,
                        admin_token=admin_token,
                        org_id=org_id,
                        project_name=child_name,
                        timeout_seconds=60,
                    )
                except RuntimeError:
                    logger.warning("Child project %s still exists in Keycloak after deletion", child_name)

        if parent_project_id:
            _delete_project_teardown(
                jwt_grpc_tenant1_admin, project_id=parent_project_id, project_name=parent_name
            )
            # Verify parent removed from Keycloak with polling
            if not skip_keycloak_sync_checks and admin_token and org_id:
                try:
                    wait_for_project_not_in_keycloak(
                        keycloak_url=keycloak_url,
                        admin_token=admin_token,
                        org_id=org_id,
                        project_name=parent_name,
                        timeout_seconds=60,
                    )
                except RuntimeError:
                    logger.warning("Parent project %s still exists in Keycloak after deletion", parent_name)
