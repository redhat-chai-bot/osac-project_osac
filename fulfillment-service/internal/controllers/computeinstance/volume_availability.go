package computeinstance

import (
	"context"
	"encoding/json"
	"fmt"
	"maps"
	"time"

	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/fieldmaskpb"

	"github.com/osac-project/osac/fulfillment-service/internal/controllers"
	osacv1alpha1 "github.com/osac-project/osac/osac-operator/api/v1alpha1"
	"github.com/osac-project/osac/osac-operator/pkg/provisioning"
	privatev1 "github.com/osac-project/osac/proto/gen/osac/private/v1"
)

const diskReadinessAnnotation = "osac.openshift.io/disk-readiness"

type diskReadiness struct {
	ConfigVersion string            `json:"configVersion"`
	DiskVolumeIDs map[string]string `json:"diskVolumeIDs"`
}

// For 0.4 only: ticket - OSAC-7670
// Bridge initial inventory availability from playbook evidence. Remove this when
// storage provisioning takes over CI disks; this is not a disk health observer.
func (t *task) reconcileVolumeAvailability(
	ctx context.Context, object *osacv1alpha1.ComputeInstance, expected osacv1alpha1.ComputeInstanceSpec,
) error {
	if t.computeInstance.GetMetadata().HasDeletionTimestamp() || object == nil ||
		!object.DeletionTimestamp.IsZero() || len(expected.DiskVolumeIDs) == 0 {
		return nil
	}
	proven, err := hasCurrentDiskReadiness(object, expected)
	if err != nil {
		return controllers.RequeueAfter(err, time.Second)
	}
	if !proven {
		return nil
	}
	return t.markInitialVolumesAvailable(ctx, expected.DiskVolumeIDs)
}

func hasCurrentDiskReadiness(object *osacv1alpha1.ComputeInstance, expected osacv1alpha1.ComputeInstanceSpec) (bool, error) {
	version, err := provisioning.ComputeDesiredConfigVersion(expected)
	if err != nil {
		return false, err
	}
	actualVersion, err := provisioning.ComputeDesiredConfigVersion(object.Spec)
	if err != nil {
		return false, err
	}
	if version != actualVersion || version != object.Status.DesiredConfigVersion ||
		version != object.Status.ProvisionConfigVersion ||
		!object.IsStatusConditionTrue(osacv1alpha1.ComputeInstanceConditionConfigurationApplied) {
		return false, nil
	}
	succeeded := false
	for _, job := range object.Status.Jobs {
		if job.Task == "provision" && job.Manager == osacv1alpha1.NetworkJobManagerNone {
			succeeded = job.State == osacv1alpha1.NetworkJobStateSucceeded
			break
		}
	}
	var evidence diskReadiness
	return succeeded && json.Unmarshal([]byte(object.Annotations[diskReadinessAnnotation]), &evidence) == nil &&
		evidence.ConfigVersion == version && maps.Equal(evidence.DiskVolumeIDs, expected.DiskVolumeIDs), nil
}

func (t *task) markInitialVolumesAvailable(ctx context.Context, diskVolumeIDs map[string]string) error {
	entries := t.computeInstance.GetStatus().GetVolumes()
	if t.registeredVolumes != nil {
		entries = t.registeredVolumes
	}
	if len(entries) != len(diskVolumeIDs) {
		return nil
	}
	seenKeys := make(map[string]bool, len(entries))
	seenIDs := make(map[string]bool, len(entries))
	volumes := make([]*privatev1.Volume, 0, len(entries))
	for _, entry := range entries {
		key, volumeID := entry.GetDiskKey(), entry.GetVolumeId()
		if volumeID == "" || diskVolumeIDs[key] != volumeID || seenKeys[key] || seenIDs[volumeID] {
			return nil
		}
		seenKeys[key], seenIDs[volumeID] = true, true
		response, getErr := t.r.volumesClient.Get(ctx, privatev1.VolumesGetRequest_builder{Id: volumeID}.Build())
		if getErr != nil {
			return controllers.RequeueAfter(fmt.Errorf("read disk inventory: %w", getErr), time.Second)
		}
		volume := response.GetObject()
		if err := validateVolumeRecordForParent(volume, t.computeInstance, key, volumeID); err != nil {
			return controllers.RequeueAfter(err, time.Second)
		}
		if !volume.GetMetadata().HasDeletionTimestamp() &&
			volume.GetStatus().GetState() == privatev1.VolumeState_VOLUME_STATE_UNSPECIFIED {
			return controllers.RequeueAfter(fmt.Errorf("disk inventory defaults are pending for %q", volumeID), time.Second)
		}
		volumes = append(volumes, volume)
	}
	for _, volume := range volumes {
		if volume.GetMetadata().HasDeletionTimestamp() ||
			volume.GetStatus().GetState() != privatev1.VolumeState_VOLUME_STATE_CREATING {
			continue
		}
		updated := proto.Clone(volume).(*privatev1.Volume)
		updated.GetStatus().SetState(privatev1.VolumeState_VOLUME_STATE_AVAILABLE)
		updated.GetStatus().SetStateTransitionTime(controllers.NowTruncated())
		updated.GetStatus().ClearMessage()
		_, err := t.r.volumesClient.Update(ctx, privatev1.VolumesUpdateRequest_builder{
			Object: updated,
			Lock:   true,
			UpdateMask: &fieldmaskpb.FieldMask{Paths: []string{
				"status.state", "status.state_transition_time", "status.message",
			}},
		}.Build())
		if err != nil {
			return controllers.RequeueAfter(fmt.Errorf("write disk inventory: %w", err), time.Second)
		}
	}
	return nil
}
