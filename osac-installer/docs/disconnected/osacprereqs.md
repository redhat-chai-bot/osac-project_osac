# Disconnected Installation Guide

This guide covers installing OSAC on OpenShift clusters that operate in
disconnected (air-gapped) or restricted-network environments where direct
access to the default Red Hat operator catalogs is unavailable.

## Overview

OSAC depends on several operators delivered through the Operator Lifecycle
Manager (OLM). In a connected environment the `osac-deps` Helm chart
installs OLM `Subscription` resources that reference the default
`redhat-operators` CatalogSource in the `openshift-marketplace` namespace.

In a disconnected environment you must:

1. Mirror the required operator images and catalog to a local registry.
2. Create a custom `CatalogSource` that points to the mirrored catalog.
3. Tell the `osac-deps` chart to use that CatalogSource instead of the
   default.

## Required Operator Packages

The following operator packages must be available in the mirrored catalog:

| Operator | Package Name | Default Channel |
|----------|-------------|-----------------|
| Ansible Automation Platform | `ansible-automation-platform-operator` | `stable-2.6-cluster-scoped` |
| AMQ Streams (Kafka) | `amq-streams` | `stable` |
| cert-manager | `openshift-cert-manager-operator` | `stable-v1` |
| OpenShift Virtualization (CNV) | `kubevirt-hyperconverged` | `stable` |
| LVM Storage | `lvms-operator` | `stable-4.22` |
| Multicluster Engine | `multicluster-engine` | `stable-2.17` |
| MetalLB | `metallb-operator` | `stable` |

> **Note:** Not every operator is required for every deployment. Only the
> operators enabled in your `values.yaml` need to be present in the
> mirrored catalog.

## Mirroring with oc-mirror

Use `oc-mirror` to build a mirror of the required catalogs. Below is a
minimal `ImageSetConfiguration` that includes all OSAC operator packages:

```yaml
kind: ImageSetConfiguration
apiVersion: mirror.openshift.io/v1alpha2
mirror:
  operators:
    - catalog: registry.redhat.io/redhat/redhat-operator-index:v4.17
      packages:
        - name: ansible-automation-platform-operator
        - name: amq-streams
        - name: openshift-cert-manager-operator
        - name: kubevirt-hyperconverged
        - name: lvms-operator
        - name: multicluster-engine
        - name: metallb-operator
```

Adjust the catalog index tag (`v4.17`) to match your OpenShift version.

Run the mirror:

```bash
oc mirror --config imageset-config.yaml \
  docker://registry.example.com/mirror
```

After mirroring, `oc-mirror` generates the mirror-set resources applicable
to your image set — an `ImageDigestMirrorSet` for digest-referenced images
and/or an `ImageTagMirrorSet` for tag-referenced images (on OCP < 4.13 the
legacy `ImageContentSourcePolicy` is generated instead). It also produces a
`CatalogSource` manifest. Apply all generated resources to the cluster:

```bash
oc apply -f oc-mirror-workspace/results-*/
```

Note the `name` and `namespace` of the generated `CatalogSource` — you
will need them in the next step.

## Overriding CatalogSource Values

The `osac-deps` chart exposes two values that control which CatalogSource
every operator Subscription references:

| Value | Default | Description |
|-------|---------|-------------|
| `catalogSource` | `redhat-operators` | Name of the OLM `CatalogSource` CR |
| `catalogSourceNamespace` | `openshift-marketplace` | Namespace where the CatalogSource exists |

### Helm install / upgrade

Pass the overrides directly:

```bash
helm upgrade --install osac-deps osac-installer/charts/osac-deps \
  --set catalogSource=my-mirror-catalog \
  --set catalogSourceNamespace=openshift-marketplace
```

### Values file

Alternatively, create a custom values file:

```yaml
# custom-values.yaml
catalogSource: my-mirror-catalog
catalogSourceNamespace: openshift-marketplace
```

Then reference it during install:

```bash
helm upgrade --install osac-deps osac-installer/charts/osac-deps \
  -f custom-values.yaml
```

## CaaS Cluster Considerations

On CaaS (Container-as-a-Service) managed clusters the mirrored catalog
may already be configured by the platform team. Check for existing
CatalogSource resources:

```bash
oc get catalogsources -A
```

Use the CatalogSource that contains the Red Hat operator content (often
named something like `cs-redhat-operator-index`). Set both values
accordingly:

```bash
--set catalogSource=cs-redhat-operator-index \
--set catalogSourceNamespace=openshift-marketplace
```

## Troubleshooting

### Subscription stuck in "UpgradePending" or no InstallPlan created

Verify the CatalogSource is healthy:

```bash
oc get catalogsource -n openshift-marketplace
```

The `READY` column should show `true` and `STATUS` should be `READY`.

### Operator package not found

Confirm the package exists in your mirrored catalog:

```bash
CATALOG_NAME="my-mirror-catalog"   # replace with your CatalogSource name
oc get packagemanifests -l "catalog=$CATALOG_NAME"
```

If the package is missing, update your `ImageSetConfiguration` to include
it and re-run `oc-mirror`.

### Image pull errors

Ensure the mirror-set resources generated by `oc-mirror`
(`ImageDigestMirrorSet` and/or `ImageTagMirrorSet`) are applied and that
the cluster nodes can reach the mirror registry. Check node-level pull
status:

```bash
oc get machineconfigpool
oc debug node/<node-name> -- chroot /host podman pull <image>
```
