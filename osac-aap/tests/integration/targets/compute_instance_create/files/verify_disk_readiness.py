"""Exercise readiness tasks on Kind; CDI phases are fixtures, not provider proof."""

import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

from kubernetes import client, config, watch

OSAC_AAP = Path(__file__).resolve().parents[5]
READINESS = "osac.openshift.io/disk-readiness"
CONFIG = "osac.openshift.io/reconciled-config-version"
VOLUME_ID = "osac.openshift.io/storage-volume-id"


class DiskReadinessTests(unittest.TestCase):
    def setUp(self):
        kubeconfig = os.environ.get("KUBECONFIG")
        if not kubeconfig:
            raise RuntimeError("Set KUBECONFIG explicitly to the disposable test cluster")
        config.load_kube_config(config_file=kubeconfig)
        self.core = client.CoreV1Api()
        self.custom = client.CustomObjectsApi()
        self.namespace = "disk-readiness-" + uuid.uuid4().hex[:12]
        self.volumes = []
        self.core.create_namespace({"metadata": {"name": self.namespace}})
        self.addCleanup(self.cleanup)
        self.mapping = {"boot": "volume-boot", "disk-1": "volume-data"}
        self.resource = {
            "apiVersion": "osac.openshift.io/v1alpha1",
            "kind": "ComputeInstance",
            "metadata": {
                "name": "readiness-vm", "namespace": self.namespace,
                "annotations": {READINESS: "stale", CONFIG: "prior-config"},
            },
            "spec": {
                "templateID": "osac.templates.ocp_virt_vm",
                "vcpus": 1, "memoryGiB": 2,
                "image": {"sourceType": "registry",
                          "sourceRef": "quay.io/containerdisks/fedora:latest"},
                "bootDisk": {"sizeGiB": 1, "storageTier": "local"},
                "additionalDisks": [{"sizeGiB": 1, "storageTier": "local"}],
                "diskVolumeIDs": self.mapping, "runStrategy": "Halted",
            },
            "status": {"desiredConfigVersion": "input-config"},
        }
        self.custom.create_namespaced_custom_object(
            "osac.openshift.io", "v1alpha1", self.namespace,
            "computeinstances", self.resource,
        )
        for name, volume_id in self.disks():
            self.custom.create_namespaced_custom_object(
                "cdi.kubevirt.io", "v1beta1", self.namespace, "datavolumes", {
                    "apiVersion": "cdi.kubevirt.io/v1beta1", "kind": "DataVolume",
                    "metadata": {"name": name, "annotations": {VOLUME_ID: volume_id}},
                    "spec": {"source": {"blank": {}}, "pvc": {
                        "storageClassName": "", "accessModes": ["ReadWriteOnce"],
                        "resources": {"requests": {"storage": "1Gi"}},
                    }},
                },
            )
            self.core.create_namespaced_persistent_volume_claim(self.namespace, {
                "metadata": {"name": name},
                "spec": {"storageClassName": "", "accessModes": ["ReadWriteOnce"],
                         "resources": {"requests": {"storage": "1Gi"}}},
            })

    def disks(self):
        return [("readiness-vm-root-disk", self.mapping["boot"]),
                ("readiness-vm-disk-1", self.mapping["disk-1"])]

    def cleanup(self):
        self.core.delete_namespace(self.namespace)
        for name in self.volumes:
            self.core.delete_persistent_volume(name)

    def annotations(self):
        return self.custom.get_namespaced_custom_object(
            "osac.openshift.io", "v1alpha1", self.namespace,
            "computeinstances", "readiness-vm",
        )["metadata"].get("annotations", {})

    def run_readiness(self, strategy):
        snapshot = copy.deepcopy(self.resource)
        snapshot["spec"]["runStrategy"] = strategy
        play = [{"hosts": "localhost", "gather_facts": False, "vars": {
            "ansible_connection": "local", "ansible_python_interpreter": sys.executable,
            "compute_instance": snapshot, "compute_instance_name": "readiness-vm",
            "compute_instance_target_namespace": self.namespace,
            "vm_disk_volume_ids": self.mapping, "vm_additional_disks": [{"size": "1Gi"}],
            "vm_run_strategy": strategy, "disk_readiness_retries": 1,
            "disk_readiness_delay": 0,
        }, "tasks": [{"name": "Run actual readiness consumer", "ansible.builtin.include_role": {
            "name": "osac.templates.ocp_virt_vm", "tasks_from": "create_disk_readiness.yaml",
        }}]}]
        environment = os.environ.copy()
        environment.update({"ANSIBLE_CONFIG": str(OSAC_AAP / "ansible.cfg"),
                            "ANSIBLE_STDOUT_CALLBACK": "default",
                            "ANSIBLE_COLLECTIONS_PATH": f"{OSAC_AAP / 'collections'}:{OSAC_AAP / 'vendor'}"})
        environment.pop("OSAC_REMOTE_CLUSTER_KUBECONFIG", None)
        for key in list(environment):
            if key.startswith("K8S_AUTH_"):
                environment.pop(key)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "readiness.json"
            path.write_text(json.dumps(play))
            return subprocess.run(
                ["ansible-playbook", "-i", "localhost,", str(path)], cwd=OSAC_AAP,
                env=environment, capture_output=True, text=True, timeout=120, check=False,
            )

    def wait_for_bound_claim(self, name):
        listing = self.core.list_namespaced_persistent_volume_claim(
            self.namespace, field_selector=f"metadata.name={name}",
        )
        if listing.items[0].status.phase != "Bound":
            watcher = watch.Watch()
            events = watcher.stream(
                self.core.list_namespaced_persistent_volume_claim, self.namespace,
                field_selector=f"metadata.name={name}",
                resource_version=listing.metadata.resource_version, timeout_seconds=45,
            )
            try:
                for event in events:
                    if event["object"].status.phase == "Bound":
                        break
            finally:
                try:
                    watcher.stop()
                finally:
                    events.close()
        self.assertEqual(self.core.read_namespaced_persistent_volume_claim(
            name, self.namespace).status.phase, "Bound")

    def test_deferred_then_ready_snapshot_and_annotation_removal(self):
        halted = self.run_readiness("Halted")
        self.assertEqual(halted.returncode, 0, halted.stdout + halted.stderr)
        self.assertNotIn(READINESS, self.annotations())
        self.assertEqual(self.annotations()[CONFIG], "prior-config")
        for name, _ in self.disks():
            claim = self.core.read_namespaced_persistent_volume_claim(name, self.namespace)
            self.assertNotEqual(claim.status.phase, "Bound")
            volume_name = self.namespace + "-" + name
            self.core.create_persistent_volume({
                "metadata": {"name": volume_name},
                "spec": {"capacity": {"storage": "1Gi"}, "accessModes": ["ReadWriteOnce"],
                         "storageClassName": "", "hostPath": {"path": "/tmp/readiness-fixture"},
                         "claimRef": {"namespace": self.namespace, "name": name}},
            })
            self.volumes.append(volume_name)
            self.wait_for_bound_claim(name)
            self.custom.patch_namespaced_custom_object_status(
                "cdi.kubevirt.io", "v1beta1", self.namespace, "datavolumes", name,
                {"status": {"phase": "Succeeded", "claimName": name}},
            )
        self.custom.patch_namespaced_custom_object_status(
            "osac.openshift.io", "v1alpha1", self.namespace, "computeinstances", "readiness-vm",
            {"status": {"desiredConfigVersion": "later-live-config"}},
        )
        ready = self.run_readiness("Always")
        self.assertEqual(ready.returncode, 0, ready.stdout + ready.stderr)
        self.assertEqual(json.loads(self.annotations()[READINESS]), {
            "configVersion": "input-config", "diskVolumeIDs": self.mapping,
        })
        self.assertEqual(self.annotations()[CONFIG], "prior-config")
        self.custom.patch_namespaced_custom_object(
            "cdi.kubevirt.io", "v1beta1", self.namespace, "datavolumes", "readiness-vm-disk-1",
            {"metadata": {"annotations": {VOLUME_ID: "different-volume"}}},
        )
        mismatch = self.run_readiness("Always")
        self.assertNotEqual(mismatch.returncode, 0, mismatch.stdout + mismatch.stderr)
        self.assertNotIn(READINESS, self.annotations())
        self.assertEqual(self.annotations()[CONFIG], "prior-config")


if __name__ == "__main__":
    unittest.main(verbosity=2)
