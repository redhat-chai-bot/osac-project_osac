/*
Copyright (c) 2026 Red Hat Inc.

Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except in compliance with the
License. You may obtain a copy of the License at

  http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software distributed under the License is distributed on an
"AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License for the specific
language governing permissions and limitations under the License.
*/

// Reproduction test for OSAC-3480: Tenant delete stuck on unnamed root project.
//
// Each tenant has an auto-created root project with an empty name and UNSPECIFIED
// state. During tenant deletion, countRemainingProjects counts this root project
// in the remaining total. Even after deleteRootProject issues a Delete API call,
// the root project may still appear in the count if its deletion has not fully
// completed (e.g., soft-delete with finalizers holding it). The delete() function
// blocks on "project(s) pending deletion" without making progress.
//
// The expected behavior is that after the reconciler initiates deletion of the
// root project, countRemainingProjects should not block tenant deletion on the
// root project. This test verifies that the tenant deletion flow properly
// handles the root project and does not get stuck on it.
//
// This test currently FAILS because countRemainingProjects blocks on any
// non-zero count without distinguishing root projects (whose deletion was
// already initiated) from user-created projects (which require admin action).

package tenant

import (
	"context"

	. "github.com/onsi/ginkgo/v2"
	. "github.com/onsi/gomega"
	"go.uber.org/mock/gomock"
	"google.golang.org/protobuf/types/known/timestamppb"

	privatev1 "github.com/osac-project/osac/fulfillment-service/internal/api/osac/private/v1"
	"github.com/osac-project/osac/fulfillment-service/internal/controllers/finalizers"
	"github.com/osac-project/osac/fulfillment-service/internal/idp"
)

var _ = Describe("OSAC-3480: Root project should not block tenant deletion", func() {
	var (
		ctx                context.Context
		ctrl               *gomock.Controller
		mockClient         *idp.MockClientInterface
		mockProjectsClient *MockProjectsClient
		idpManager         *idp.TenantManager
		reconciler         *function
	)

	BeforeEach(func() {
		var err error
		ctx = context.Background()
		ctrl = gomock.NewController(GinkgoT())
		mockClient = idp.NewMockClientInterface(ctrl)
		mockProjectsClient = NewMockProjectsClient(ctrl)

		idpManager, err = idp.NewTenantManager().
			SetLogger(logger).
			SetClient(mockClient).
			Build()
		Expect(err).ToNot(HaveOccurred())

		reconciler = &function{
			logger:         logger,
			projectsClient: mockProjectsClient,
			idpManager:     idpManager,
		}
	})

	It("should not block tenant deletion when root project deletion is still in progress", func() {
		// Tenant marked for deletion.
		tenant := privatev1.Tenant_builder{
			Id: "org-root-stuck",
			Metadata: privatev1.Metadata_builder{
				Name:              "test-org",
				Finalizers:        []string{finalizers.Controller},
				DeletionTimestamp: timestamppb.Now(),
			}.Build(),
			Status: privatev1.TenantStatus_builder{
				State:         privatev1.TenantState_TENANT_STATE_SYNCED,
				IdpTenantName: "test-org",
			}.Build(),
		}.Build()

		// The unnamed root project exists with UNSPECIFIED state.
		rootProject := privatev1.Project_builder{
			Id: "root-proj-id",
			Metadata: privatev1.Metadata_builder{
				Name:   "",
				Tenant: "test-org",
			}.Build(),
			Status: privatev1.ProjectStatus_builder{
				State: privatev1.ProjectState_PROJECT_STATE_UNSPECIFIED,
			}.Build(),
		}.Build()

		gomock.InOrder(
			// deleteRootProject: finds the root project and calls Delete.
			mockProjectsClient.EXPECT().
				List(gomock.Any(), gomock.Any()).
				Return(privatev1.ProjectsListResponse_builder{
					Items: []*privatev1.Project{rootProject},
					Total: 1,
				}.Build(), nil),

			// deleteRootProject: Delete call succeeds (soft-delete, sets deletion_timestamp).
			mockProjectsClient.EXPECT().
				Delete(gomock.Any(), gomock.Any()).
				Return(privatev1.ProjectsDeleteResponse_builder{}.Build(), nil),

			// countRemainingProjects: the root project still appears in the count because
			// its deletion has not fully completed. In a real system with finalizers, the
			// project exists with a deletion_timestamp, but the server may still report
			// Total: 1 in the response metadata (depending on how the server applies the
			// filter). This simulates the root project being the only remaining project.
			mockProjectsClient.EXPECT().
				List(gomock.Any(), gomock.Any()).
				Return(privatev1.ProjectsListResponse_builder{
					Total: 1,
				}.Build(), nil),
		)

		// We expect that after initiating root project deletion, the tenant
		// deletion should proceed to the next step (vault/IDP cleanup) rather
		// than blocking on "project(s) pending deletion".
		mockClient.EXPECT().
			DeleteTenant(gomock.Any(), "test-org").
			Return(nil).
			MaxTimes(1)

		t := &task{r: reconciler, tenant: tenant}
		err := t.delete(ctx)

		// The expected behavior is that after deleteRootProject initiated
		// deletion of the root project, the tenant delete should NOT be
		// blocked. The root project is a system-managed resource whose
		// deletion was already initiated — it should not require admin
		// action like user-created projects do.
		//
		// This currently FAILS with "tenant still has 1 project(s) pending
		// deletion" because countRemainingProjects does not distinguish
		// the root project (whose deletion was initiated by the reconciler)
		// from user-created projects.
		Expect(err).ToNot(HaveOccurred(),
			"root project should not block tenant deletion after its deletion was initiated")
		Expect(tenant.GetMetadata().GetFinalizers()).ToNot(ContainElement(finalizers.Controller),
			"finalizer should be removed after successful deletion")
	})
})
