"""Test-only remote bytes live separately from ARTIFACT_ROOT; never a production fallback."""

import os
from pathlib import Path

import creator_service.object_storage as objects


class RemoteRuntimeStorage(objects.LocalStorageBackend):
    def __init__(self) -> None:
        super().__init__(str(Path(os.environ["ARTIFACT_ROOT"]).parent / "remote-objects"))
        self.provider = os.environ["STORAGE_BACKEND"]


objects._storage_backend = RemoteRuntimeStorage()

from shorts_api.main import app as app  # noqa: E402
