package computeinstance

import (
	"context"
	"encoding/json"
	"errors"
	"maps"

	. "github.com/onsi/ginkgo/v2"
	. "github.com/onsi/gomega"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	"github.com/osac-project/osac/fulfillment-service/internal/controllers"
	osacv1alpha1 "github.com/osac-project/osac/osac-operator/api/v1alpha1"
	"github.com/osac-project/osac/osac-operator/pkg/provisioning"
	privatev1 "github.com/osac-project/osac/proto/gen/osac/private/v1"
)

type availabilityVolumesClient struct {
	privatev1.VolumesClient
	volumes      map[string]*privatev1.Volume
	writes       []*privatev1.VolumesUpdateRequest
	failID       string
	readErr      error
	beforeUpdate func(string)
}

func (client *availabilityVolumesClient) Get(
	_ context.Context, request *privatev1.VolumesGetRequest, _ ...grpc.CallOption,
) (*privatev1.VolumesGetResponse, error) {
	if client.readErr != nil {
		return nil, client.readErr
	}
	return privatev1.VolumesGetResponse_builder{Object: client.volumes[request.GetId()]}.Build(), nil
}

func (client *availabilityVolumesClient) Update(
	_ context.Context, request *privatev1.VolumesUpdateRequest, _ ...grpc.CallOption,
) (*privatev1.VolumesUpdateResponse, error) {
	client.writes = append(client.writes, proto.Clone(request).(*privatev1.VolumesUpdateRequest))
	if client.beforeUpdate != nil {
		client.beforeUpdate(request.GetObject().GetId())
	}
	if request.GetObject().GetId() == client.failID {
		return nil, errors.New("retry write")
	}
	Expect(request.GetLock()).To(BeTrue())
	if request.GetObject().GetMetadata().GetVersion() != client.volumes[request.GetObject().GetId()].GetMetadata().GetVersion() {
		return nil, status.Error(codes.Aborted, "inventory version changed")
	}
	Expect(request.GetUpdateMask().GetPaths()).To(ConsistOf("status.state", "status.state_transition_time", "status.message"))
	client.volumes[request.GetObject().GetId()] = proto.Clone(request.GetObject()).(*privatev1.Volume)
	return privatev1.VolumesUpdateResponse_builder{Object: request.GetObject()}.Build(), nil
}

func availabilityFixture() (*task, *osacv1alpha1.ComputeInstance, osacv1alpha1.ComputeInstanceSpec, *availabilityVolumesClient) {
	instance := privatev1.ComputeInstance_builder{
		Id:       "instance-1",
		Metadata: privatev1.Metadata_builder{Tenant: "tenant-1", Project: "project-1"}.Build(),
		Status:   &privatev1.ComputeInstanceStatus{},
	}.Build()
	client := &availabilityVolumesClient{volumes: map[string]*privatev1.Volume{}}
	spec := osacv1alpha1.ComputeInstanceSpec{DiskVolumeIDs: map[string]string{"boot": "volume-boot", "disk-1": "volume-data"}}
	for _, key := range []string{"boot", "disk-1"} {
		volumeID := spec.DiskVolumeIDs[key]
		instance.GetStatus().SetVolumes(append(instance.GetStatus().GetVolumes(),
			privatev1.ComputeInstanceVolume_builder{DiskKey: key, VolumeId: volumeID}.Build()))
		client.volumes[volumeID] = privatev1.Volume_builder{
			Id: volumeID,
			Metadata: privatev1.Metadata_builder{
				Name: "ci-instance-1-" + key, Tenant: "tenant-1", Project: "project-1", Creator: "system", Version: 1,
			}.Build(),
			Status: privatev1.VolumeStatus_builder{State: privatev1.VolumeState_VOLUME_STATE_CREATING, Message: proto.String("pending")}.Build(),
		}.Build()
	}
	version, err := provisioning.ComputeDesiredConfigVersion(spec)
	Expect(err).NotTo(HaveOccurred())
	evidence, err := json.Marshal(diskReadiness{ConfigVersion: version, DiskVolumeIDs: maps.Clone(spec.DiskVolumeIDs)})
	Expect(err).NotTo(HaveOccurred())
	object := &osacv1alpha1.ComputeInstance{
		ObjectMeta: metav1.ObjectMeta{Annotations: map[string]string{diskReadinessAnnotation: string(evidence)}},
		Spec:       spec,
		Status: osacv1alpha1.ComputeInstanceStatus{
			DesiredConfigVersion: version, ProvisionConfigVersion: version,
			Jobs: []osacv1alpha1.NetworkJobStatus{{Task: "provision", Manager: osacv1alpha1.NetworkJobManagerNone, State: osacv1alpha1.NetworkJobStateSucceeded}},
		},
	}
	object.SetStatusCondition(osacv1alpha1.ComputeInstanceConditionConfigurationApplied, metav1.ConditionTrue, "", osacv1alpha1.ReasonAsExpected)
	return &task{r: &function{volumesClient: client}, computeInstance: instance}, object, spec, client
}

var _ = Describe("initial disk inventory availability", func() {
	It("marks verified boot and data available once and clears messages", func() {
		task, object, spec, client := availabilityFixture()
		Expect(task.reconcileVolumeAvailability(context.Background(), object, spec)).To(Succeed())
		Expect(client.writes).To(HaveLen(2))
		for _, volume := range client.volumes {
			Expect(volume.GetStatus().GetState()).To(Equal(privatev1.VolumeState_VOLUME_STATE_AVAILABLE))
			Expect(volume.GetStatus().HasMessage()).To(BeFalse())
			Expect(volume.GetStatus().GetStateTransitionTime()).NotTo(BeNil())
		}
		Expect(task.reconcileVolumeAvailability(context.Background(), object, spec)).To(Succeed())
		Expect(client.writes).To(HaveLen(2))
	})

	DescribeTable("rejects stale or unverified completion", func(change func(*task, *osacv1alpha1.ComputeInstance, *osacv1alpha1.ComputeInstanceSpec)) {
		task, object, spec, client := availabilityFixture()
		change(task, object, &spec)
		Expect(task.reconcileVolumeAvailability(context.Background(), object, spec)).To(Succeed())
		Expect(client.writes).To(BeEmpty())
	},
		Entry("no attestation (including deferred Halted and custom overrides)", func(_ *task, object *osacv1alpha1.ComputeInstance, _ *osacv1alpha1.ComputeInstanceSpec) {
			delete(object.Annotations, diskReadinessAnnotation)
		}),
		Entry("malformed attestation", func(_ *task, object *osacv1alpha1.ComputeInstance, _ *osacv1alpha1.ComputeInstanceSpec) {
			object.Annotations[diskReadinessAnnotation] = "invalid"
		}),
		Entry("old desired version", func(_ *task, object *osacv1alpha1.ComputeInstance, _ *osacv1alpha1.ComputeInstanceSpec) {
			object.Status.DesiredConfigVersion = "old"
		}),
		Entry("old provision version", func(_ *task, object *osacv1alpha1.ComputeInstance, _ *osacv1alpha1.ComputeInstanceSpec) {
			object.Status.ProvisionConfigVersion = "old"
		}),
		Entry("running job", func(_ *task, object *osacv1alpha1.ComputeInstance, _ *osacv1alpha1.ComputeInstanceSpec) {
			object.Status.Jobs[0].State = osacv1alpha1.NetworkJobStateRunning
		}),
		Entry("failed job", func(_ *task, object *osacv1alpha1.ComputeInstance, _ *osacv1alpha1.ComputeInstanceSpec) {
			object.Status.Jobs[0].State = osacv1alpha1.NetworkJobStateFailed
		}),
		Entry("no provision job", func(_ *task, object *osacv1alpha1.ComputeInstance, _ *osacv1alpha1.ComputeInstanceSpec) {
			object.Status.Jobs = nil
		}),
		Entry("configuration not applied", func(_ *task, object *osacv1alpha1.ComputeInstance, _ *osacv1alpha1.ComputeInstanceSpec) {
			object.SetStatusCondition(osacv1alpha1.ComputeInstanceConditionConfigurationApplied, metav1.ConditionFalse, "", "")
		}),
		Entry("API spec changed before hub feedback", func(_ *task, _ *osacv1alpha1.ComputeInstance, spec *osacv1alpha1.ComputeInstanceSpec) {
			spec.RunStrategy = osacv1alpha1.RunStrategyAlways
		}),
		Entry("new disk ID with old success", func(_ *task, _ *osacv1alpha1.ComputeInstance, spec *osacv1alpha1.ComputeInstanceSpec) {
			spec.DiskVolumeIDs = map[string]string{"boot": "new-volume"}
		}),
		Entry("attested mapping differs", func(_ *task, object *osacv1alpha1.ComputeInstance, _ *osacv1alpha1.ComputeInstanceSpec) {
			payload, err := json.Marshal(diskReadiness{ConfigVersion: object.Status.DesiredConfigVersion, DiskVolumeIDs: map[string]string{"boot": "wrong"}})
			Expect(err).NotTo(HaveOccurred())
			object.Annotations[diskReadinessAnnotation] = string(payload)
		}),
		Entry("attested config differs", func(_ *task, object *osacv1alpha1.ComputeInstance, spec *osacv1alpha1.ComputeInstanceSpec) {
			payload, err := json.Marshal(diskReadiness{ConfigVersion: "old", DiskVolumeIDs: spec.DiskVolumeIDs})
			Expect(err).NotTo(HaveOccurred())
			object.Annotations[diskReadinessAnnotation] = string(payload)
		}),
		Entry("parent deleting", func(task *task, _ *osacv1alpha1.ComputeInstance, _ *osacv1alpha1.ComputeInstanceSpec) {
			task.computeInstance.GetMetadata().SetDeletionTimestamp(timestamppb.Now())
		}),
		Entry("hub CI deleting", func(_ *task, object *osacv1alpha1.ComputeInstance, _ *osacv1alpha1.ComputeInstanceSpec) {
			now := metav1.Now()
			object.DeletionTimestamp = &now
		}),
		Entry("persisted mapping incomplete", func(task *task, _ *osacv1alpha1.ComputeInstance, _ *osacv1alpha1.ComputeInstanceSpec) {
			task.computeInstance.GetStatus().SetVolumes(task.computeInstance.GetStatus().GetVolumes()[:1])
		}),
	)

	DescribeTable("validates every record before writing any", func(change func(*privatev1.Volume)) {
		task, object, spec, client := availabilityFixture()
		change(client.volumes["volume-data"])
		err := task.reconcileVolumeAvailability(context.Background(), object, spec)
		Expect(err).To(HaveOccurred())
		Expect(controllers.IsRequeueable(err)).To(BeTrue())
		Expect(client.writes).To(BeEmpty())
	},
		Entry("wrong ID", func(volume *privatev1.Volume) { volume.SetId("wrong") }),
		Entry("wrong name", func(volume *privatev1.Volume) { volume.GetMetadata().SetName("ci-other-disk-1") }),
		Entry("wrong tenant", func(volume *privatev1.Volume) { volume.GetMetadata().SetTenant("other") }),
		Entry("wrong project", func(volume *privatev1.Volume) { volume.GetMetadata().SetProject("other") }),
		Entry("wrong creator", func(volume *privatev1.Volume) { volume.GetMetadata().SetCreator("other") }),
	)

	DescribeTable("does not overwrite other lifecycle states", func(state privatev1.VolumeState) {
		task, object, spec, client := availabilityFixture()
		client.volumes["volume-data"].GetStatus().SetState(state)
		Expect(task.reconcileVolumeAvailability(context.Background(), object, spec)).To(Succeed())
		Expect(client.volumes["volume-data"].GetStatus().GetState()).To(Equal(state))
		Expect(client.writes).To(HaveLen(1))
	},
		Entry("deleting", privatev1.VolumeState_VOLUME_STATE_DELETING),
		Entry("deleted", privatev1.VolumeState_VOLUME_STATE_DELETED),
		Entry("failed (no recovery)", privatev1.VolumeState_VOLUME_STATE_FAILED),
		Entry("already available", privatev1.VolumeState_VOLUME_STATE_AVAILABLE),
	)

	It("skips a record with deletion metadata even if it still says creating", func() {
		task, object, spec, client := availabilityFixture()
		client.volumes["volume-data"].GetMetadata().SetDeletionTimestamp(timestamppb.Now())
		Expect(task.reconcileVolumeAvailability(context.Background(), object, spec)).To(Succeed())
		Expect(client.writes).To(HaveLen(1))
	})

	It("requeues partial writes without rewriting successful transitions or creating disks", func() {
		task, object, spec, client := availabilityFixture()
		client.failID = "volume-data"
		err := task.reconcileVolumeAvailability(context.Background(), object, spec)
		Expect(controllers.IsRequeueable(err)).To(BeTrue())
		transition := proto.Clone(client.volumes["volume-boot"].GetStatus().GetStateTransitionTime())
		client.failID = ""
		Expect(task.reconcileVolumeAvailability(context.Background(), object, spec)).To(Succeed())
		Expect(client.writes).To(HaveLen(3))
		Expect(proto.Equal(transition, client.volumes["volume-boot"].GetStatus().GetStateTransitionTime())).To(BeTrue())
		Expect(task.reconcileVolumeAvailability(context.Background(), object, spec)).To(Succeed())
		Expect(client.writes).To(HaveLen(3))
	})

	It("requeues read failures without any state writes", func() {
		task, object, spec, client := availabilityFixture()
		client.readErr = errors.New("retry read")
		err := task.reconcileVolumeAvailability(context.Background(), object, spec)
		Expect(controllers.IsRequeueable(err)).To(BeTrue())
		Expect(client.writes).To(BeEmpty())
	})

	It("requeues until inventory defaults are persisted without skipping initial availability", func() {
		task, object, spec, client := availabilityFixture()
		client.volumes["volume-data"].GetStatus().SetState(privatev1.VolumeState_VOLUME_STATE_UNSPECIFIED)
		Expect(controllers.IsRequeueable(task.reconcileVolumeAvailability(context.Background(), object, spec))).To(BeTrue())
		Expect(client.writes).To(BeEmpty())
		client.volumes["volume-data"].GetStatus().SetState(privatev1.VolumeState_VOLUME_STATE_CREATING)
		Expect(task.reconcileVolumeAvailability(context.Background(), object, spec)).To(Succeed())
		Expect(client.writes).To(HaveLen(2))
	})

	It("uses optimistic locking so a concurrent deletion cannot be overwritten", func() {
		task, object, spec, client := availabilityFixture()
		client.beforeUpdate = func(volumeID string) {
			volume := proto.Clone(client.volumes[volumeID]).(*privatev1.Volume)
			volume.GetMetadata().SetVersion(2)
			volume.GetStatus().SetState(privatev1.VolumeState_VOLUME_STATE_DELETING)
			client.volumes[volumeID] = volume
		}
		Expect(controllers.IsRequeueable(task.reconcileVolumeAvailability(context.Background(), object, spec))).To(BeTrue())
		Expect(client.volumes["volume-boot"].GetStatus().GetState()).To(Equal(privatev1.VolumeState_VOLUME_STATE_DELETING))
		client.beforeUpdate = nil
		Expect(task.reconcileVolumeAvailability(context.Background(), object, spec)).To(Succeed())
		Expect(client.volumes["volume-boot"].GetStatus().GetState()).To(Equal(privatev1.VolumeState_VOLUME_STATE_DELETING))
	})
})
