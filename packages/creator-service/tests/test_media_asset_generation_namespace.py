from uuid import UUID
from io import BytesIO

import pytest

from creator_service.media_asset_service import InMemoryMediaAssetStorage, MediaAssetService
from creator_service.object_storage import LocalStorageBackend
from PIL import Image


@pytest.mark.asyncio
async def test_repeated_uploads_always_use_fresh_uuid_keys_before_metadata_is_visible(tmp_path):
    # Given repeated uploads of identical filenames and bytes to one workspace.
    class RecordingStorage(LocalStorageBackend):
        def __init__(self):
            super().__init__(str(tmp_path))
            self.uploads = []

        def upload(self, key, data, content_type="application/octet-stream"):
            assert key not in self.uploads
            assert any(intent.storage_key == key for intent in metadata._intents.values())
            assert all(asset["storage_key"] != key for asset in metadata._assets)
            self.uploads.append(key)
            return super().upload(key, data, content_type)

    storage = RecordingStorage()
    metadata = InMemoryMediaAssetStorage()
    service = MediaAssetService(storage_backend=storage, asset_storage=metadata)
    image = BytesIO()
    Image.new("RGB", (4, 6)).save(image, format="PNG")
    keys = []
    # When the platform creates new asset identities rather than overwriting old ones.
    for _ in range(20):
        asset = await service.create_image_asset(
            workspace_id=7, project_id=1, filename="same.png", data=image.getvalue(), content_type="image/png",
        )
        keys.append(asset.storage_key)
    # Then every backend write uses a distinct UUID-addressed workspace asset key.
    assert len(storage.uploads) == 20
    assert len(set(keys)) == 20
    assert storage.uploads == keys
    for key in keys:
        assert key.startswith("workspaces/7/assets/")
        identifier = key.rsplit("/", 1)[1].split("-", 1)[0]
        assert UUID(hex=identifier).version == 4
