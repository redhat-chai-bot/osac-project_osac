/*
Copyright (c) 2026 Red Hat, Inc.

Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except
in compliance with the License. You may obtain a copy of the License at

  http://www.apache.org/licenses/LICENSE-2.0
*/

package watch

import (
	"testing"

	cloudevents "github.com/cloudevents/sdk-go/v2"
	"github.com/osac-project/osac-metering/internal/projection"
)

func TestBuildComponentEventHandlesNilBaseData(t *testing.T) {
	c := &Consumer{}
	baseCE := cloudevents.NewEvent()
	baseCE.SetID("evt-1")
	baseCE.SetSource("osac-metering")
	baseCE.SetType("osac.resource.started.v1")
	// No SetData call: the base event carries zero-length data, so DataAs
	// leaves the target map nil without returning an error.

	ce, err := c.buildComponentEvent(&baseCE, "evt-1/node-a", map[string]any{"component": "worker"})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	var data map[string]any
	if err := ce.DataAs(&data); err != nil {
		t.Fatalf("unexpected error reading component event data: %v", err)
	}
	if data["billing_dimensions"] == nil {
		t.Errorf("expected billing_dimensions to be set on the component event even when the base event carried no data")
	}
}

func TestProjectionIsAheadTreatsTombstoneAsTerminal(t *testing.T) {
	existing := &projection.ResourceState{
		ResourceID:         "bmi-tombstoned",
		Deleted:            true,
		FulfillmentVersion: 4,
		CurrentState:       "RUNNING",
	}

	// Non-delete events should always be blocked for tombstoned resources.
	for _, version := range []int32{1, 4, 5, 100} {
		if !projectionIsAhead(existing, version, "RUNNING", nil, false, false) {
			t.Errorf("projectionIsAhead(isDeleteEvent=false) = false for tombstone at incoming version %d; want true", version)
		}
	}
}

func TestProjectionIsAheadAllowsDeleteThroughTombstone(t *testing.T) {
	existing := &projection.ResourceState{
		ResourceID:         "vm-tombstoned",
		Deleted:            true,
		FulfillmentVersion: 4,
		CurrentState:       "RUNNING",
	}

	// A delete event at the same or later version must pass through so
	// the lifecycle deleted.v1 metering event is published. This is the
	// fix for the reconciler-vs-Watch race condition: reconcileMissedDeletions
	// tombstones the projection before the Watch delete event arrives.
	for _, version := range []int32{4, 5, 100} {
		if projectionIsAhead(existing, version, "RUNNING", nil, false, true) {
			t.Errorf("projectionIsAhead(isDeleteEvent=true) = true for tombstone at version %d; want false (should allow delete through)", version)
		}
	}

	// A truly stale delete event (older fulfillment version) must still be blocked.
	for _, version := range []int32{1, 2, 3} {
		if !projectionIsAhead(existing, version, "RUNNING", nil, false, true) {
			t.Errorf("projectionIsAhead(isDeleteEvent=true) = false for tombstone at stale version %d; want true", version)
		}
	}
}
