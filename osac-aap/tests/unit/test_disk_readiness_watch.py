"""Exercise the integration wait's real Watch API with transport fixtures, not Kind."""

import importlib.util
import io
import json
from pathlib import Path

import pytest
from kubernetes import client, watch
from urllib3.response import HTTPResponse


SCRIPT = (Path(__file__).resolve().parents[1] / "integration/targets/compute_instance_create"
          / "files/verify_disk_readiness.py")
SPEC = importlib.util.spec_from_file_location("verify_disk_readiness", SCRIPT)
readiness = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(readiness)


class WatchResponse(HTTPResponse):
    def __init__(self, events, error):
        super().__init__(body=io.BytesIO(), preload_content=False)
        self.events = events
        self.error = error
        self.close_calls = 0
        self.release_calls = 0

    def stream(self, **kwargs):
        for phase in self.events:
            yield (json.dumps({"type": "MODIFIED", "object": {
                "apiVersion": "v1", "kind": "PersistentVolumeClaim",
                "metadata": {"name": "claim", "resourceVersion": "8"},
                "status": {"phase": phase},
            }}) + "\n").encode()
        if self.error:
            raise self.error

    def close(self):
        self.close_calls += 1
        super().close()

    def release_conn(self):
        self.release_calls += 1
        super().release_conn()


@pytest.fixture
def binding_wait(monkeypatch):
    def setup(*, initial="Pending", events=("Pending", "Bound"), final="Bound", error=None):
        response = WatchResponse(events, error)
        requests = []
        stopped = []
        original_stop = watch.Watch.stop

        def stop(watcher):
            original_stop(watcher)
            stopped.append(watcher)

        def get(url, *, query_params, **kwargs):
            parameters = dict(query_params)
            requests.append((url, parameters, kwargs["_preload_content"]))
            if parameters.get("watch"):
                return response
            if url.endswith("/claim"):
                document = {"status": {"phase": final}}
            else:
                document = {"metadata": {"resourceVersion": "7"},
                            "items": [{"status": {"phase": initial}}]}
            return HTTPResponse(body=json.dumps(document).encode(), status=200)

        api_client = client.ApiClient(configuration=client.Configuration())
        monkeypatch.setattr(api_client.rest_client, "GET", get)
        monkeypatch.setattr(watch.Watch, "stop", stop)
        scenario = readiness.DiskReadinessTests()
        scenario.namespace = "fixture"
        scenario.core = client.CoreV1Api(api_client)
        return scenario, response, requests, stopped

    return setup


def assert_watch_cleanup(response, requests, stopped):
    assert len(stopped) == 1
    assert stopped[0]._stop
    assert response.close_calls == 1
    assert response.release_calls == 1
    assert requests[0][0].endswith("/namespaces/fixture/persistentvolumeclaims")
    assert requests[0][1] == {"fieldSelector": "metadata.name=claim"}
    assert requests[1][1] == {"fieldSelector": "metadata.name=claim",
                              "resourceVersion": "7", "timeoutSeconds": 45, "watch": True}
    assert requests[1][2] is False


def test_pending_then_bound_stops_and_closes_real_watch(binding_wait):
    scenario, response, requests, stopped = binding_wait(error=RuntimeError("unread tail"))
    scenario.wait_for_bound_claim("claim")
    assert_watch_cleanup(response, requests, stopped)
    assert len(requests) == 3
    assert requests[-1][0].endswith("/persistentvolumeclaims/claim")


def test_already_bound_skips_watch_but_reads_current_claim(binding_wait):
    scenario, response, requests, stopped = binding_wait(initial="Bound")
    scenario.wait_for_bound_claim("claim")
    assert len(requests) == 2
    assert requests[-1][0].endswith("/persistentvolumeclaims/claim")
    assert not stopped
    assert response.close_calls == response.release_calls == 0


@pytest.mark.parametrize("events", [(), ("Pending",)])
def test_exhausted_watch_cannot_approve_unbound_claim(binding_wait, events):
    scenario, response, requests, stopped = binding_wait(events=events, final="Pending")
    with pytest.raises(AssertionError, match="Pending.*Bound"):
        scenario.wait_for_bound_claim("claim")
    assert_watch_cleanup(response, requests, stopped)
    assert len(requests) == 3


def test_exhausted_watch_uses_final_read_as_truth(binding_wait):
    scenario, response, requests, stopped = binding_wait(events=())
    scenario.wait_for_bound_claim("claim")
    assert_watch_cleanup(response, requests, stopped)
    assert len(requests) == 3


@pytest.mark.parametrize("initial,events", [("Bound", ()), ("Pending", ("Bound",))])
def test_final_read_rejects_unbound_even_after_bound_evidence(binding_wait, initial, events):
    scenario, response, requests, stopped = binding_wait(initial=initial, events=events, final="Pending")
    with pytest.raises(AssertionError, match="Pending.*Bound"):
        scenario.wait_for_bound_claim("claim")
    assert requests[-1][0].endswith("/persistentvolumeclaims/claim")
    if initial != "Bound":
        assert_watch_cleanup(response, requests, stopped)


def test_stream_exception_propagates_after_cleanup(binding_wait):
    scenario, response, requests, stopped = binding_wait(events=("Pending",), error=RuntimeError("transport"))
    with pytest.raises(RuntimeError, match="transport"):
        scenario.wait_for_bound_claim("claim")
    assert_watch_cleanup(response, requests, stopped)
    assert len(requests) == 2
