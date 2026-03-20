from __future__ import annotations

from urllib.error import HTTPError

import pytest

from check_lund_access import TileInfo, require_credentials, verify_asset_access


class DummyResponse:
    def __init__(self, status: int, content_length: str = "1") -> None:
        self.status = status
        self.headers = {"Content-Length": content_length}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None


class DummyOpener:
    def __init__(self, response: DummyResponse | None = None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error

    def open(self, request, timeout: int = 30):
        if self.error is not None:
            raise self.error
        return self.response


@pytest.fixture
def sample_tile() -> TileInfo:
    return TileInfo(
        item_id="617_38_5050",
        collection_id="mhm-61_3",
        href="https://dl1.lantmateriet.se/hojd/data/grid1m/61_3/55/61750_3850_25.tif",
        size_bytes=9_286_509,
        bbox_3006=(0.0, 0.0, 1.0, 1.0),
    )


def test_require_credentials_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LANTMATERIET_USERNAME", "user@example.com")
    monkeypatch.setenv("LANTMATERIET_PASSWORD", "secret")
    assert require_credentials() == ("user@example.com", "secret")


def test_require_credentials_missing_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LANTMATERIET_USERNAME", raising=False)
    monkeypatch.delenv("LANTMATERIET_PASSWORD", raising=False)
    with pytest.raises(RuntimeError, match="Missing required environment variable"):
        require_credentials()


def test_verify_asset_access_accepts_partial_content(
    monkeypatch: pytest.MonkeyPatch,
    sample_tile: TileInfo,
) -> None:
    monkeypatch.setattr(
        "check_lund_access.urllib.request.build_opener",
        lambda *_args, **_kwargs: DummyOpener(DummyResponse(status=206, content_length="1")),
    )
    status_code, content_length = verify_asset_access(sample_tile, "user@example.com", "secret")
    assert status_code == 206
    assert content_length == 1


def test_verify_asset_access_raises_on_http_error(
    monkeypatch: pytest.MonkeyPatch,
    sample_tile: TileInfo,
) -> None:
    http_error = HTTPError(sample_tile.href, 401, "Unauthorized", hdrs=None, fp=None)
    monkeypatch.setattr(
        "check_lund_access.urllib.request.build_opener",
        lambda *_args, **_kwargs: DummyOpener(error=http_error),
    )
    with pytest.raises(RuntimeError, match="status 401"):
        verify_asset_access(sample_tile, "user@example.com", "secret")
