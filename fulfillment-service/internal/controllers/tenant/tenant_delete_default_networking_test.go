/*
Copyright (c) 2026 Red Hat Inc.

Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except in compliance with the
License. You may obtain a copy of the License at

  http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software distributed under the License is distributed on an
"AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License for the specific
language governing permissions and limitations under the License.
*/

// Reproduction test for OSAC-4513: Tenant delete stuck on default networking.
//
// The tenant reconciler's delete() function waits for all projects to be deleted
// but never initiates deletion of default networking resources (VirtualNetworks,
// Subnets, SecurityGroups) that carry the label osac.openshift.io/default=true.
// The validateNotDefault check in the networking servers blocks manual deletion of
// these resources with FailedPrecondition. The expected behavior is that the
// tenant delete flow should clean up default networking resources before or as
// part of the deletion process.
//
// This test verifies that default networking resources are cleaned up during
// tenant deletion. It currently FAILS because the delete() function never
// touches VirtualNetworks, Subnets, or SecurityGroups clients.

package tenant

import (
	"context"
	"fmt"

	. "github.com/onsi/ginkgo/v2"
	. "github.com/onsi/gomega"
	"go.uber.org/mock/gomock"
	"google.golang.org/protobuf/types/known/timestamppb"

	privatev1 "github.com/osac-project/osac/fulfillment-service/internal/api/osac/private/v1"
	"github.com/osac-project/osac/fulfillment-service/internal/controllers/finalizers"
	"github.com/osac-project/osac/fulfillment-service/internal/idp"
)

var _ = Describe("OSAC-4513: Default networking cleanup during tenant deletion", func() {
	var (
		ctx                context.Context
		ctrl               *gomock.Controller
		mockClient         *idp.MockClientInterface
		mockProjectsClient *MockProjectsClient
		mockVNsClient      *MockVirtualNetworksClient
		mockSubnetsClient  *MockSubnetsClient
		mockSGsClient      *MockSecurityGroupsClient
		idpManager         *idp.TenantManager
		reconciler         *function
	)

	BeforeEach(func() {
		var err error
		ctx = context.Background()
		ctrl = gomock.NewController(GinkgoT())
		mockClient = idp.NewMockClientInterface(ctrl)
		mockProjectsClient = NewMockProjectsClient(ctrl)
		mockVNsClient = NewMockVirtualNetworksClient(ctrl)
		mockSubnetsClient = NewMockSubnetsClient(ctrl)
		mockSGsClient = NewMockSecurityGroupsClient(ctrl)

		idpManager, err = idp.NewTenantManager().
			SetLogger(logger).
			SetClient(mockClient).
			Build()
		Expect(err).ToNot(HaveOccurred())

		reconciler = &function{
			logger:                logger,
			projectsClient:        mockProjectsClient,
			virtualNetworksClient: mockVNsClient,
			subnetsClient:         mockSubnetsClient,
			securityGroupsClient:  mockSGsClient,
			idpManager:            idpManager,
		}
	})

	It("should initiate deletion of default networking resources during tenant deletion", func() {
		tenant := privatev1.Tenant_builder{
			Id: "org-net-cleanup",
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

		// Set up default networking resources with the osac.openshift.io/default=true label.
		defaultLabels := map[string]string{defaultLabelKey: "true"}

		defaultVN := privatev1.VirtualNetwork_builder{
			Id: "default-vn-id",
			Metadata: privatev1.Metadata_builder{
				Name:   "default",
				Tenant: "test-org",
				Labels: defaultLabels,
			}.Build(),
			Status: privatev1.VirtualNetworkStatus_builder{
				State: privatev1.VirtualNetworkState_VIRTUAL_NETWORK_STATE_READY,
			}.Build(),
		}.Build()

		defaultSubnet := privatev1.Subnet_builder{
			Id: "default-subnet-id",
			Metadata: privatev1.Metadata_builder{
				Name:   "default-ipv4",
				Tenant: "test-org",
				Labels: defaultLabels,
			}.Build(),
			Status: privatev1.SubnetStatus_builder{
				State: privatev1.SubnetState_SUBNET_STATE_READY,
			}.Build(),
		}.Build()

		defaultSG := privatev1.SecurityGroup_builder{
			Id: "default-sg-id",
			Metadata: privatev1.Metadata_builder{
				Name:   "default",
				Tenant: "test-org",
				Labels: defaultLabels,
			}.Build(),
			Status: privatev1.SecurityGroupStatus_builder{
				State: privatev1.SecurityGroupState_SECURITY_GROUP_STATE_READY,
			}.Build(),
		}.Build()

		// The delete flow should list default networking resources and initiate their deletion.
		mockVNsClient.EXPECT().
			List(gomock.Any(), gomock.Any()).
			Return(privatev1.VirtualNetworksListResponse_builder{
				Items: []*privatev1.VirtualNetwork{defaultVN},
			}.Build(), nil).
			AnyTimes()

		mockVNsClient.EXPECT().
			Delete(gomock.Any(), gomock.Any()).
			DoAndReturn(func(_ context.Context, req *privatev1.VirtualNetworksDeleteRequest, _ ...interface{}) (*privatev1.VirtualNetworksDeleteResponse, error) {
				Expect(req.GetId()).To(Equal("default-vn-id"))
				return privatev1.VirtualNetworksDeleteResponse_builder{}.Build(), nil
			}).
			Times(1)

		mockSubnetsClient.EXPECT().
			List(gomock.Any(), gomock.Any()).
			Return(privatev1.SubnetsListResponse_builder{
				Items: []*privatev1.Subnet{defaultSubnet},
			}.Build(), nil).
			AnyTimes()

		mockSubnetsClient.EXPECT().
			Delete(gomock.Any(), gomock.Any()).
			DoAndReturn(func(_ context.Context, req *privatev1.SubnetsDeleteRequest, _ ...interface{}) (*privatev1.SubnetsDeleteResponse, error) {
				Expect(req.GetId()).To(Equal("default-subnet-id"))
				return privatev1.SubnetsDeleteResponse_builder{}.Build(), nil
			}).
			Times(1)

		mockSGsClient.EXPECT().
			List(gomock.Any(), gomock.Any()).
			Return(privatev1.SecurityGroupsListResponse_builder{
				Items: []*privatev1.SecurityGroup{defaultSG},
			}.Build(), nil).
			AnyTimes()

		mockSGsClient.EXPECT().
			Delete(gomock.Any(), gomock.Any()).
			DoAndReturn(func(_ context.Context, req *privatev1.SecurityGroupsDeleteRequest, _ ...interface{}) (*privatev1.SecurityGroupsDeleteResponse, error) {
				Expect(req.GetId()).To(Equal("default-sg-id"))
				return privatev1.SecurityGroupsDeleteResponse_builder{}.Build(), nil
			}).
			Times(1)

		// Standard delete flow mocks: no root projects, no remaining projects.
		// deleteRootProject List call
		mockProjectsClient.EXPECT().
			List(gomock.Any(), gomock.Any()).
			Return(privatev1.ProjectsListResponse_builder{}.Build(), nil).
			Times(1)
		// countRemainingProjects List call
		mockProjectsClient.EXPECT().
			List(gomock.Any(), gomock.Any()).
			Return(privatev1.ProjectsListResponse_builder{Total: 0}.Build(), nil).
			Times(1)

		// IDP deletion succeeds.
		mockClient.EXPECT().
			DeleteTenant(gomock.Any(), "test-org").
			Return(nil).
			Times(1)

		t := &task{r: reconciler, tenant: tenant}
		err := t.delete(ctx)

		// The delete flow should complete without error, having cleaned up
		// default networking resources. This currently FAILS because delete()
		// never calls Delete on VirtualNetworks, Subnets, or SecurityGroups.
		Expect(err).ToNot(HaveOccurred(),
			fmt.Sprintf("delete() should clean up default networking resources, but got: %v", err))
		Expect(tenant.GetMetadata().GetFinalizers()).ToNot(ContainElement(finalizers.Controller),
			"finalizer should be removed after successful deletion including networking cleanup")
	})
})
