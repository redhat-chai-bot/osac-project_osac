"""Check initial DataVolume readiness against persisted inventory identity."""

from collections.abc import Callable


def disk_volume_initialized(resources: list[dict], volume_id: str) -> bool:
    if not volume_id or len(resources) != 1:
        return False
    volume = resources[0]
    annotations = volume.get("metadata", {}).get("annotations") or {}
    return (
        annotations.get("osac.openshift.io/storage-volume-id") == volume_id
        and volume.get("status", {}).get("phase") == "Succeeded"
    )


class FilterModule:
    def filters(self) -> dict[str, Callable]:
        return {"disk_volume_initialized": disk_volume_initialized}
