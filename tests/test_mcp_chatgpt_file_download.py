"""Tests for ChatGPT file download support for artifacts and sources."""

from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import unquote

from notebooklm_tools.mcp.tools import _utils, downloads, sources


def test_source_get_content_appends_download_url(tmp_path):
    """source_get_content must copy content to PUBLIC_DIR and append download_url if mcp_base_url is set."""
    PUBLIC_DIR = tmp_path / "public_artifacts"

    mock_client = MagicMock()
    mock_content = {
        "content": "This is raw source content.",
        "title": "Example Source Document",
        "source_type": "text",
        "char_count": 27,
    }

    with (
        patch("notebooklm_tools.mcp.tools.sources.get_client", return_value=mock_client),
        patch(
            "notebooklm_tools.mcp.tools.sources.sources_service.get_source_content",
            return_value=mock_content,
        ),
        patch("notebooklm_tools.mcp.tools.sources.PUBLIC_DIR", PUBLIC_DIR),
    ):
        # 1. Test without base_url set
        _utils.mcp_base_url.set("")
        res1 = sources.source_get_content(source_id="src_123")
        assert res1["status"] == "success"
        assert "download_url" not in res1

        # 2. Test with base_url set
        token = _utils.mcp_base_url.set("https://tunnel.example")
        try:
            res2 = sources.source_get_content(source_id="src_123")
            assert res2["status"] == "success"
            assert "download_url" in res2
            assert res2["download_url"].startswith("https://tunnel.example/artifacts/")

            # Verify file was written to public dir
            filename = unquote(Path(res2["download_url"]).name)
            pub_file = PUBLIC_DIR / filename
            assert pub_file.exists()
            assert pub_file.read_text(encoding="utf-8") == "This is raw source content."
        finally:
            _utils.mcp_base_url.reset(token)


def test_download_artifact_appends_download_url(tmp_path):
    """download_artifact must copy downloaded artifact to PUBLIC_DIR and append download_url if mcp_base_url is set."""
    PUBLIC_DIR = tmp_path / "public_artifacts"

    # Create a dummy downloaded file
    download_dir = tmp_path / "downloads"
    download_dir.mkdir()
    dummy_file = download_dir / "artifact.mp3"
    dummy_file.write_bytes(b"dummy audio content")

    mock_client = MagicMock()
    mock_download_result = {
        "path": str(dummy_file),
        "filename": "artifact.mp3",
        "size_bytes": 19,
    }

    with (
        patch("notebooklm_tools.mcp.tools.downloads.get_client", return_value=mock_client),
        patch(
            "notebooklm_tools.mcp.tools.downloads.downloads_service.download_async",
            return_value=mock_download_result,
        ),
        patch("notebooklm_tools.mcp.tools.downloads.PUBLIC_DIR", PUBLIC_DIR),
    ):
        # 1. Test without base_url set
        _utils.mcp_base_url.set("")
        res1 = downloads.download_artifact(
            notebook_id="nb_123", artifact_type="audio", output_path="artifact.mp3"
        )
        assert res1["status"] == "success"
        assert "download_url" not in res1

        # 2. Test with base_url set
        token = _utils.mcp_base_url.set("https://tunnel.example")
        try:
            res2 = downloads.download_artifact(
                notebook_id="nb_123", artifact_type="audio", output_path="artifact.mp3"
            )
            assert res2["status"] == "success"
            assert "download_url" in res2
            assert res2["download_url"] == "https://tunnel.example/artifacts/artifact.mp3"

            # Verify file was copied to public dir
            pub_file = PUBLIC_DIR / "artifact.mp3"
            assert pub_file.exists()
            assert pub_file.read_bytes() == b"dummy audio content"
        finally:
            _utils.mcp_base_url.reset(token)


def test_source_get_content_url_encodes_download_url(tmp_path):
    """source_get_content must URL-encode filenames for public artifact links."""
    public_dir = tmp_path / "public_artifacts"
    mock_client = MagicMock()
    mock_content = {
        "content": "encoded filename content",
        "title": "Source With Spaces",
        "source_type": "text",
        "char_count": 24,
    }

    with (
        patch("notebooklm_tools.mcp.tools.sources.get_client", return_value=mock_client),
        patch(
            "notebooklm_tools.mcp.tools.sources.sources_service.get_source_content",
            return_value=mock_content,
        ),
        patch("notebooklm_tools.mcp.tools.sources.PUBLIC_DIR", public_dir),
    ):
        token = _utils.mcp_base_url.set("https://tunnel.example")
        try:
            result = sources.source_get_content(source_id="src_123")
        finally:
            _utils.mcp_base_url.reset(token)

    assert result["status"] == "success"
    assert result["download_url"] == "https://tunnel.example/artifacts/Source%20With%20Spaces.txt"
