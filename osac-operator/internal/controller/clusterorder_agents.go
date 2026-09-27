/*
Copyright 2026.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

package controller

import (
	"context"
	"fmt"

	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/labels"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/selection"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	ctrllog "sigs.k8s.io/controller-runtime/pkg/log"

	"github.com/osac-project/osac/osac-operator/api/v1alpha1"
)

var agentGVK = schema.GroupVersionKind{
	Group:   "agent-install.openshift.io",
	Version: "v1beta1",
	Kind:    "Agent",
}

const (
	agentClusterOrderLabel   = "osac.openshift.io/clusterorder"
	agentInstanceTypeLabel   = "osac.openshift.io/baremetal_instance_type"
	agentResourceClassLabel  = "osac.openshift.io/resource_class" // Deprecated: use agentInstanceTypeLabel
	agentServerNameLabel     = "netris.server/name"
	agentClaimedByHypershift = "agent-install.openshift.io/clusterdeployment-namespace"

	defaultAgentNamespace = "hardware-inventory"
)

// reconcileAgentSelection selects available agents for each node set and labels
// them so HyperShift's NodePool can claim them. Returns Requeue if not enough
// agents are available yet.
func (r *ClusterOrderReconciler) reconcileAgentSelection(
	ctx context.Context, instance *v1alpha1.ClusterOrder,
) (ctrl.Result, error) {
	log := ctrllog.FromContext(ctx)

	if len(instance.Spec.NodeRequests) == 0 {
		return ctrl.Result{}, nil
	}

	// Check if agents are already selected
	if len(instance.Status.NodeSets) > 0 {
		// Backfill FabricInterface from spec into existing NodeSetStatus
		// entries that don't have it yet (e.g. created before the field
		// was added to NodeRequest).
		for i := range instance.Status.NodeSets {
			if instance.Status.NodeSets[i].FabricInterface != "" {
				continue
			}
			for _, nr := range instance.Spec.NodeRequests {
				if nr.EffectiveInstanceType() == instance.Status.NodeSets[i].Name && nr.FabricInterface != "" {
					instance.Status.NodeSets[i].FabricInterface = nr.FabricInterface
					break
				}
			}
		}
		return ctrl.Result{}, nil
	}

	agentNamespace := r.AgentNamespace
	if agentNamespace == "" {
		agentNamespace = defaultAgentNamespace
	}

	var nodeSets []v1alpha1.NodeSetStatus

	for _, nodeReq := range instance.Spec.NodeRequests {
		instanceType := nodeReq.EffectiveInstanceType()
		agents, err := r.selectAgents(ctx, agentNamespace, instance.Name, instanceType, nodeReq.NumberOfNodes)
		if err != nil {
			return ctrl.Result{}, err
		}

		if len(agents) < nodeReq.NumberOfNodes {
			log.Info("not enough agents available, requeueing",
				"instanceType", instanceType,
				"requested", nodeReq.NumberOfNodes,
				"available", len(agents),
			)
			return ctrl.Result{RequeueAfter: defaultPreconditionRequeueInterval}, nil
		}

		// Label the selected agents
		for _, agent := range agents {
			if err := r.labelAgent(ctx, agent, instance.Name); err != nil {
				return ctrl.Result{}, fmt.Errorf("labeling agent %s: %w", agent.GetName(), err)
			}
		}

		// Build agent status entries
		var agentStatuses []v1alpha1.AgentStatus
		for _, agent := range agents {
			agentStatuses = append(agentStatuses, v1alpha1.AgentStatus{
				AgentName: agent.GetName(),
				HostName:  agent.GetLabels()[agentServerNameLabel],
			})
		}

		nodeSets = append(nodeSets, v1alpha1.NodeSetStatus{
			Name:            instanceType,
			FabricInterface: nodeReq.FabricInterface,
			Agents:          agentStatuses,
		})
	}

	instance.Status.NodeSets = nodeSets
	log.Info("agent selection complete", "nodeSets", len(nodeSets))
	return ctrl.Result{}, nil
}

// selectAgents lists available agents matching the resource class that are not
// already allocated to a cluster.
func (r *ClusterOrderReconciler) selectAgents(
	ctx context.Context, namespace, clusterOrderName, resourceClass string, count int,
) ([]*unstructured.Unstructured, error) {
	// First check if agents are already labeled for this cluster order
	alreadyLabeled, err := r.listAgentsForClusterOrder(ctx, namespace, clusterOrderName, resourceClass)
	if err != nil {
		return nil, err
	}
	if len(alreadyLabeled) >= count {
		return alreadyLabeled[:count], nil
	}

	// Find available agents matching the instance type.
	// During the transition period, agents may carry either the new
	// agentInstanceTypeLabel or the deprecated agentResourceClassLabel.
	// Try the new label first; fall back to the old one.
	selector := labels.NewSelector()
	rcReq, err := labels.NewRequirement(agentInstanceTypeLabel, selection.Equals, []string{resourceClass})
	if err != nil {
		return nil, fmt.Errorf("invalid instance type %q for label selector: %w", resourceClass, err)
	}
	selector = selector.Add(*rcReq)
	noClusterOrder, err := labels.NewRequirement(agentClusterOrderLabel, selection.DoesNotExist, nil)
	if err != nil {
		return nil, fmt.Errorf("building label requirement: %w", err)
	}
	selector = selector.Add(*noClusterOrder)
	noClaimed, err := labels.NewRequirement(agentClaimedByHypershift, selection.DoesNotExist, nil)
	if err != nil {
		return nil, fmt.Errorf("building label requirement: %w", err)
	}
	selector = selector.Add(*noClaimed)

	agentList := &unstructured.UnstructuredList{}
	agentList.SetGroupVersionKind(schema.GroupVersionKind{
		Group:   agentGVK.Group,
		Version: agentGVK.Version,
		Kind:    agentGVK.Kind + "List",
	})

	if err := r.Client.List(ctx, agentList,
		client.InNamespace(namespace),
		client.MatchingLabelsSelector{Selector: selector},
	); err != nil {
		return nil, fmt.Errorf("listing available agents for instance type %s: %w", resourceClass, err)
	}

	// Combine already-labeled + newly available, up to count
	result := append(alreadyLabeled, make([]*unstructured.Unstructured, 0, count-len(alreadyLabeled))...)
	for i := range agentList.Items {
		if len(result) >= count {
			break
		}
		result = append(result, &agentList.Items[i])
	}

	// Fallback: if we still need more agents, try the deprecated label
	if len(result) < count {
		fallbackSelector := labels.NewSelector()
		rcFallback, err := labels.NewRequirement(agentResourceClassLabel, selection.Equals, []string{resourceClass})
		if err != nil {
			return nil, fmt.Errorf("building fallback label requirement: %w", err)
		}
		fallbackSelector = fallbackSelector.Add(*rcFallback)
		// Exclude agents that already carry the new label (already found above)
		noNewLabel, err := labels.NewRequirement(agentInstanceTypeLabel, selection.DoesNotExist, nil)
		if err != nil {
			return nil, fmt.Errorf("building label requirement: %w", err)
		}
		fallbackSelector = fallbackSelector.Add(*noNewLabel)
		fallbackSelector = fallbackSelector.Add(*noClusterOrder)
		fallbackSelector = fallbackSelector.Add(*noClaimed)

		fallbackList := &unstructured.UnstructuredList{}
		fallbackList.SetGroupVersionKind(schema.GroupVersionKind{
			Group:   agentGVK.Group,
			Version: agentGVK.Version,
			Kind:    agentGVK.Kind + "List",
		})
		if err := r.Client.List(ctx, fallbackList,
			client.InNamespace(namespace),
			client.MatchingLabelsSelector{Selector: fallbackSelector},
		); err != nil {
			return nil, fmt.Errorf("listing available agents (fallback) for instance type %s: %w", resourceClass, err)
		}
		for i := range fallbackList.Items {
			if len(result) >= count {
				break
			}
			result = append(result, &fallbackList.Items[i])
		}
	}

	return result, nil
}

// listAgentsForClusterOrder returns agents already labeled for this cluster order.
// Checks both the new instance type label and the deprecated resource class label.
func (r *ClusterOrderReconciler) listAgentsForClusterOrder(
	ctx context.Context, namespace, clusterOrderName, resourceClass string,
) ([]*unstructured.Unstructured, error) {
	// Try new label first
	matchLabels := map[string]string{
		agentClusterOrderLabel: clusterOrderName,
		agentInstanceTypeLabel: resourceClass,
	}

	agentList := &unstructured.UnstructuredList{}
	agentList.SetGroupVersionKind(schema.GroupVersionKind{
		Group:   agentGVK.Group,
		Version: agentGVK.Version,
		Kind:    agentGVK.Kind + "List",
	})

	if err := r.Client.List(ctx, agentList,
		client.InNamespace(namespace),
		client.MatchingLabels(matchLabels),
	); err != nil {
		return nil, fmt.Errorf("listing agents for cluster order %s: %w", clusterOrderName, err)
	}

	var result []*unstructured.Unstructured
	for i := range agentList.Items {
		result = append(result, &agentList.Items[i])
	}

	// Fallback: also check agents labeled with the deprecated resource class label
	fallbackLabels := map[string]string{
		agentClusterOrderLabel:  clusterOrderName,
		agentResourceClassLabel: resourceClass,
	}
	fallbackList := &unstructured.UnstructuredList{}
	fallbackList.SetGroupVersionKind(schema.GroupVersionKind{
		Group:   agentGVK.Group,
		Version: agentGVK.Version,
		Kind:    agentGVK.Kind + "List",
	})
	if err := r.Client.List(ctx, fallbackList,
		client.InNamespace(namespace),
		client.MatchingLabels(fallbackLabels),
	); err != nil {
		return nil, fmt.Errorf("listing agents (fallback) for cluster order %s: %w", clusterOrderName, err)
	}
	seen := make(map[string]struct{}, len(result))
	for _, a := range result {
		seen[a.GetName()] = struct{}{}
	}
	for i := range fallbackList.Items {
		if _, dup := seen[fallbackList.Items[i].GetName()]; !dup {
			result = append(result, &fallbackList.Items[i])
		}
	}

	return result, nil
}

// labelAgent sets the clusterorder label on an agent to reserve it.
func (r *ClusterOrderReconciler) labelAgent(
	ctx context.Context, agent *unstructured.Unstructured, clusterOrderName string,
) error {
	agentLabels := agent.GetLabels()
	if agentLabels[agentClusterOrderLabel] == clusterOrderName {
		return nil
	}
	if agentLabels == nil {
		agentLabels = make(map[string]string)
	}
	agentLabels[agentClusterOrderLabel] = clusterOrderName
	agent.SetLabels(agentLabels)
	return r.Client.Update(ctx, agent)
}

// reconcileAgentCleanup removes clusterorder labels from agents allocated to
// this cluster, making them available for future clusters.
func (r *ClusterOrderReconciler) reconcileAgentCleanup(
	ctx context.Context, instance *v1alpha1.ClusterOrder,
) error {
	log := ctrllog.FromContext(ctx)

	agentNamespace := r.AgentNamespace
	if agentNamespace == "" {
		agentNamespace = defaultAgentNamespace
	}

	agentList := &unstructured.UnstructuredList{}
	agentList.SetGroupVersionKind(schema.GroupVersionKind{
		Group:   agentGVK.Group,
		Version: agentGVK.Version,
		Kind:    agentGVK.Kind + "List",
	})

	if err := r.Client.List(ctx, agentList,
		client.InNamespace(agentNamespace),
		client.MatchingLabels{agentClusterOrderLabel: instance.Name},
	); err != nil {
		return fmt.Errorf("listing agents for cleanup: %w", err)
	}

	for i := range agentList.Items {
		agent := &agentList.Items[i]
		agentLabels := agent.GetLabels()
		delete(agentLabels, agentClusterOrderLabel)
		agent.SetLabels(agentLabels)
		if err := r.Client.Update(ctx, agent); err != nil {
			return fmt.Errorf("unlabeling agent %s: %w", agent.GetName(), err)
		}
		log.Info("released agent", "agent", agent.GetName())
	}

	return nil
}
