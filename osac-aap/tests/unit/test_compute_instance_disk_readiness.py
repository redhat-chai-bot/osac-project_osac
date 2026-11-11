"""Execute the real post-create readiness tasks against local Kubernetes doubles."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

OSAC_AAP = Path(__file__).resolve().parents[2]
READINESS = "osac.openshift.io/disk-readiness"
CONFIG = "osac.openshift.io/reconciled-config-version"
VOLUME_ID = "osac.openshift.io/storage-volume-id"


def _run_readiness(tmp_path, *, strategy="Always", version="input-version", phase="Succeeded",
                   pvc_phase="Bound", volume_id="volume-data", missing="", mapping=None):
    collection = tmp_path / "collections/ansible_collections/kubernetes/core/plugins/modules"
    collection.mkdir(parents=True, exist_ok=True)
    log = tmp_path / "writes.jsonl"
    log.unlink(missing_ok=True)
    disks = {"boot": "volume-boot", "disk-1": "volume-data"}
    resources = {}
    for key, expected_id in disks.items():
        name = "fixture-root-disk" if key == "boot" else "fixture-disk-1"
        resources[f"DataVolume/{name}"] = [] if missing == "DataVolume" and key == "disk-1" else [{
            "metadata": {"annotations": {VOLUME_ID: volume_id if key == "disk-1" else expected_id}},
            "status": {"phase": phase if key == "disk-1" else "Succeeded", "claimName": name + "-claim"},
        }]
        resources[f"PersistentVolumeClaim/{name}-claim"] = [] if missing == "PVC" and key == "disk-1" else [{
            "status": {"phase": pvc_phase if key == "disk-1" else "Bound"},
        }]
    fixture = tmp_path / "resources.json"
    fixture.write_text(json.dumps(resources))
    (collection / "k8s_info.py").write_text(
        "from ansible.module_utils.basic import AnsibleModule\n"
        "import json\n"
        "module = AnsibleModule(argument_spec={key: {'type': 'raw'} for key in "
        "['kubeconfig', 'api_version', 'kind', 'name', 'namespace', 'wait', 'wait_condition', 'wait_timeout']})\n"
        "assert module.params['namespace'] == 'workload-fixture'\n"
        f"resources = json.load(open({str(fixture)!r}))\n"
        "key = module.params['kind'] + '/' + module.params['name']\n"
        "module.exit_json(changed=False, resources=resources.get(key, [{}] if module.params['kind'] == 'VirtualMachine' else []))\n"
    )
    (collection / "k8s.py").write_text(
        "from ansible.module_utils.basic import AnsibleModule\n"
        "import json\n"
        "module = AnsibleModule(argument_spec={key: {'type': 'raw'} for key in "
        "['api_version', 'kind', 'name', 'namespace', 'state', 'definition']})\n"
        "assert module.params['kind'] == 'ComputeInstance'\n"
        "assert module.params['namespace'] == 'hub-fixture'\n"
        f"with open({str(log)!r}, 'a') as stream:\n"
        "    stream.write(json.dumps(module.params['definition']['metadata']['annotations']) + '\\n')\n"
        "module.exit_json(changed=True)\n"
    )
    playbook = [{
        "hosts": "localhost",
        "gather_facts": False,
        "vars": {
            "ansible_connection": "local", "ansible_python_interpreter": sys.executable,
            "compute_instance_name": "fixture", "compute_instance_target_namespace": "workload-fixture",
            "compute_instance": {"metadata": {"namespace": "hub-fixture"},
                                 "status": {"desiredConfigVersion": version}},
            "vm_disk_volume_ids": disks if mapping is None else mapping,
            "vm_additional_disks": [{"size": "10Gi"}], "vm_run_strategy": strategy,
            "disk_readiness_retries": 1, "disk_readiness_delay": 0,
            "guest_os_family": "linux", "vm_image_source": "fixture-image",
            "vm_cpu_sockets": 1, "vm_memory": "1Gi", "vm_boot_disk_size": "10Gi",
        },
        "tasks": [{"name": "Execute actual post-create consumer", "ansible.builtin.include_role": {
            "name": "osac.templates.ocp_virt_vm", "tasks_from": "create_wait_annotate.yaml",
        }}],
    }]
    path = tmp_path / "playbook.json"
    path.write_text(json.dumps(playbook))
    environment = os.environ.copy()
    environment.update({
        "ANSIBLE_CONFIG": str(OSAC_AAP / "ansible.cfg"),
        "ANSIBLE_COLLECTIONS_PATH": f"{tmp_path / 'collections'}:{OSAC_AAP / 'collections'}:{OSAC_AAP / 'vendor'}",
        "ANSIBLE_FORCE_COLOR": "false", "ANSIBLE_STDOUT_CALLBACK": "default",
    })
    result = subprocess.run(
        ["ansible-playbook", "-i", "localhost,", str(path)], cwd=OSAC_AAP, env=environment,
        capture_output=True, text=True, timeout=90, check=False,
    )
    writes = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return result, writes


def test_ready_boot_and_data_attest_the_input_config_and_exact_mapping(tmp_path):
    result, writes = _run_readiness(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert writes[0] == {READINESS: None}
    attestation = next(write[READINESS] for write in writes if write.get(READINESS))
    assert json.loads(attestation) == {
        "configVersion": "input-version", "diskVolumeIDs": {"boot": "volume-boot", "disk-1": "volume-data"},
    }
    assert writes[-1] == {CONFIG: "input-version"}


@pytest.mark.parametrize("parameters", [
    {"phase": "Pending"}, {"phase": "Failed"}, {"missing": "DataVolume"},
    {"missing": "PVC"}, {"volume_id": "wrong-volume"}, {"volume_id": ""}, {"pvc_phase": "Pending"},
    {"mapping": {"boot": "volume-boot"}},
])
def test_running_unverified_disks_fail_without_attestation(tmp_path, parameters):
    result, writes = _run_readiness(tmp_path, **parameters)
    assert result.returncode != 0, result.stdout + result.stderr
    assert writes == [{READINESS: None}]


@pytest.mark.parametrize("parameters", [
    {"phase": "WaitForFirstConsumer", "pvc_phase": "Pending"}, {"phase": "Failed"},
    {"missing": "DataVolume"}, {"missing": "PVC"}, {"volume_id": "wrong-volume"},
])
def test_halted_unverified_disks_succeed_without_changing_config_semantics(tmp_path, parameters):
    result, writes = _run_readiness(tmp_path, strategy="Halted", **parameters)
    assert result.returncode == 0, result.stdout + result.stderr
    assert writes == [{READINESS: None}, {CONFIG: "input-version"}]


def test_halted_verified_disks_can_attest_without_starting(tmp_path):
    result, writes = _run_readiness(tmp_path, strategy="Halted")
    assert result.returncode == 0, result.stdout + result.stderr
    assert any(write.get(READINESS) for write in writes)


def test_deferred_halted_then_supported_start_rechecks_with_new_input(tmp_path):
    result, writes = _run_readiness(tmp_path, strategy="Halted", phase="WaitForFirstConsumer", pvc_phase="Pending")
    assert result.returncode == 0, result.stdout + result.stderr
    assert not any(write.get(READINESS) for write in writes)
    result, writes = _run_readiness(tmp_path, strategy="Always", version="started-input-version")
    assert result.returncode == 0, result.stdout + result.stderr
    attestation = next(write[READINESS] for write in writes if write.get(READINESS))
    assert json.loads(attestation)["configVersion"] == "started-input-version"
