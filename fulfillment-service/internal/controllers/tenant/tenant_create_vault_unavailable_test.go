/*
Copyright (c) 2026 Red Hat Inc.

Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except in compliance with the
License. You may obtain a copy of the License at

  http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software distributed under the License is distributed on an
"AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License for the specific
language governing permissions and limitations under the License.
*/

// Reproduction test for OSAC-4795: Tenant create stuck UNSPECIFIED when OpenBao is absent.
//
// When the vault lifecycle client is configured but the OpenBao/Vault endpoint is
// unreachable, ensureVaultNamespace() returns an error that propagates up from
// syncToIDP(), causing Run() to return an error without persisting any state change.
// The tenant remains in UNSPECIFIED/PENDING state forever, retried endlessly by the
// controller without making progress.
//
// The expected behavior is that tenant creation should either:
// - Make vault optional (skip it and transition to SYNCED when vault is unavailable)
// - Transition to a degraded-but-usable state with a clear condition indicating vault is down
//
// This test verifies that a tenant can reach a usable state (at minimum SYNCED with
// a degraded condition on vault readiness) when vault is unavailable. It currently
// FAILS because ensureVaultNamespace propagates the error and blocks all progress.

package tenant

import (
	"context"
	"fmt"

	. "github.com/onsi/ginkgo/v2"
	. "github.com/onsi/gomega"
	"go.uber.org/mock/gomock"

	privatev1 "github.com/osac-project/osac/fulfillment-service/internal/api/osac/private/v1"
	"github.com/osac-project/osac/fulfillment-service/internal/controllers/finalizers"
	"github.com/osac-project/osac/fulfillment-service/internal/idp"
	"github.com/osac-project/osac/fulfillment-service/internal/vault"
)

var _ = Describe("OSAC-4795: Tenant creation with unavailable vault", func() {
	var (
		ctx            context.Context
		ctrl           *gomock.Controller
		mockClient     *idp.MockClientInterface
		mockVault      *vault.MockLifecycleClient
		idpManager     *idp.TenantManager
		reconciler     *function
	)

	BeforeEach(func() {
		var err error
		ctx = context.Background()
		ctrl = gomock.NewController(GinkgoT())
		mockClient = idp.NewMockClientInterface(ctrl)
		mockVault = vault.NewMockLifecycleClient(ctrl)

		idpManager, err = idp.NewTenantManager().
			SetLogger(logger).
			SetClient(mockClient).
			Build()
		Expect(err).ToNot(HaveOccurred())

		reconciler = &function{
			logger:         logger,
			idpManager:     idpManager,
			vaultLifecycle: mockVault,
		}
	})

	It("should reach a usable state when vault endpoint is unreachable during initial sync", func() {
		// Simulate a new tenant that needs initial IDP sync.
		tenant := privatev1.Tenant_builder{
			Id: "org-vault-down",
			Metadata: privatev1.Metadata_builder{
				Name:       "vault-test-org",
				Finalizers: []string{finalizers.Controller},
				Tenant:     "tenant-1",
			}.Build(),
			Status: privatev1.TenantStatus_builder{
				BreakGlassCredentials: privatev1.BreakGlassCredentials_builder{
					Username: "vault-test-org-osac-break-glass",
					Password: "test-password",
				}.Build(),
			}.Build(),
		}.Build()

		// IDP operations succeed normally.
		mockClient.EXPECT().
			CreateTenant(gomock.Any(), gomock.Any()).
			Return(&idp.Tenant{Name: "vault-test-org", Enabled: true}, nil).
			Times(1)

		mockClient.EXPECT().
			CreateUser(gomock.Any(), "vault-test-org", gomock.Any()).
			DoAndReturn(func(_ context.Context, _ string, user *idp.User) (*idp.User, error) {
				user.ID = "user-vault-down"
				return user, nil
			}).
			Times(1)

		mockClient.EXPECT().
			AssignIdpManagerPermissions(gomock.Any(), "user-vault-down").
			Return(nil).
			Times(1)

		// Vault is unreachable — simulates DNS lookup failure or connection refused.
		mockVault.EXPECT().
			EnsureTenantNamespace(gomock.Any(), "vault-test-org").
			Return(fmt.Errorf("dial tcp: lookup openbao.osac.svc.cluster.local: no such host")).
			Times(1)

		t := &task{r: reconciler, tenant: tenant}
		err := t.update(ctx)

		// The tenant should NOT be stuck — it should reach a usable state.
		// At minimum, the tenant should transition to SYNCED (with vault condition=FALSE)
		// or PENDING with a clear message about vault unavailability, NOT return an error
		// that prevents any state from being persisted.
		//
		// This currently FAILS because ensureVaultNamespace returns an error that
		// propagates up from syncToIDP, and update() returns it to Run(), which
		// then skips the state update — leaving the tenant stuck in UNSPECIFIED forever.
		Expect(err).ToNot(HaveOccurred(),
			"tenant creation should not be blocked by vault unavailability")

		state := tenant.GetStatus().GetState()
		Expect(state).To(SatisfyAny(
			Equal(privatev1.TenantState_TENANT_STATE_SYNCED),
			Equal(privatev1.TenantState_TENANT_STATE_PENDING),
		), "tenant should reach SYNCED or PENDING state, not be stuck in UNSPECIFIED")

		// Verify the IDP sync at least partially succeeded.
		Expect(tenant.GetStatus().GetIdpTenantName()).To(Equal("vault-test-org"),
			"IDP tenant name should be recorded even when vault fails")
	})

	It("should reach a usable state when vault endpoint is unreachable for synced tenant", func() {
		// Simulate a tenant that is already SYNCED but vault condition is not yet ready.
		tenant := privatev1.Tenant_builder{
			Id: "org-vault-synced",
			Metadata: privatev1.Metadata_builder{
				Name:       "synced-vault-org",
				Finalizers: []string{finalizers.Controller},
				Tenant:     "tenant-1",
			}.Build(),
			Status: privatev1.TenantStatus_builder{
				State:            privatev1.TenantState_TENANT_STATE_SYNCED,
				IdpTenantName:    "synced-vault-org",
				BreakGlassUserId: "user-synced",
			}.Build(),
		}.Build()

		// IDP update succeeds.
		mockClient.EXPECT().
			GetTenant(gomock.Any(), "synced-vault-org").
			Return(&idp.Tenant{Name: "synced-vault-org", Enabled: true}, nil).
			Times(1)
		mockClient.EXPECT().
			UpdateTenant(gomock.Any(), gomock.Any()).
			Return(&idp.Tenant{Name: "synced-vault-org", Enabled: true}, nil).
			Times(1)

		// Vault is unreachable.
		mockVault.EXPECT().
			EnsureTenantNamespace(gomock.Any(), "synced-vault-org").
			Return(fmt.Errorf("dial tcp: lookup openbao.osac.svc.cluster.local: no such host")).
			Times(1)

		t := &task{r: reconciler, tenant: tenant}
		err := t.update(ctx)

		// A SYNCED tenant should remain functional even when vault is temporarily
		// unreachable. The vault condition should be set to FALSE with a message,
		// but the tenant should stay SYNCED rather than returning an error that
		// prevents reconciliation of other tenant aspects (like default networking).
		//
		// This currently FAILS because ensureVaultNamespace propagates the error
		// and the tenant's state update is never persisted.
		Expect(err).ToNot(HaveOccurred(),
			"vault unavailability should not block reconciliation of synced tenant")

		Expect(tenant.GetStatus().GetState()).To(Equal(privatev1.TenantState_TENANT_STATE_SYNCED),
			"tenant should remain SYNCED despite vault being unavailable")
	})
})
