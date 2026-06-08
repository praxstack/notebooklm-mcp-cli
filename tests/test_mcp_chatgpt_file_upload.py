"""Tests for ChatGPT file parameter upload support."""

from pathlib import Path
from unittest.mock import patch


def test_source_add_chatgpt_file_is_registered_with_file_param_meta():
    """The ChatGPT upload bridge must advertise an OpenAI file parameter."""
    from notebooklm_tools.mcp.tools import _utils, sources  # noqa: F401

    matches = [item for item in _utils._tool_registry if item[0] == "source_add_chatgpt_file"]

    assert matches
    assert matches[0][2] == {"openai/fileParams": ["file"]}


def test_coerce_chatgpt_file_reference_accepts_json_string():
    """Some MCP clients serialize file params as JSON strings."""
    from notebooklm_tools.mcp.tools.sources import _coerce_chatgpt_file_reference

    result = _coerce_chatgpt_file_reference(
        '{"download_url":"https://example.test/file.pdf","file_id":"file_123"}'
    )

    assert result["download_url"] == "https://example.test/file.pdf"
    assert result["file_id"] == "file_123"


def test_source_add_chatgpt_file_downloads_and_uploads_then_cleans_cache(tmp_path):
    """ChatGPT files are downloaded locally, added via source_add(file), then removed."""
    from notebooklm_tools.mcp.tools import sources

    payload = b"hello from chatgpt"
    cached_paths: list[Path] = []

    class FakeHeaders(dict):
        def get(self, key, default=None):
            return super().get(key, default)

    class FakeResponse:
        headers = FakeHeaders({"Content-Length": str(len(payload))})

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _size):
            nonlocal payload
            chunk, payload = payload, b""
            return chunk

    def fake_urlopen(_request, timeout):
        assert timeout == 30
        return FakeResponse()

    def fake_source_add(**kwargs):
        path = Path(kwargs["file_path"])
        cached_paths.append(path)
        assert path.exists()
        assert path.read_bytes() == b"hello from chatgpt"
        return {
            "status": "success",
            "ready": kwargs["wait"],
            "source_type": "file",
            "source_id": "source-123",
            "title": kwargs["title"],
        }

    with (
        patch.object(sources, "CHATGPT_FILE_CACHE_DIR", tmp_path),
        patch("notebooklm_tools.mcp.tools.sources.urllib.request.urlopen", fake_urlopen),
        patch("notebooklm_tools.mcp.tools.sources.source_add", side_effect=fake_source_add),
    ):
        result = sources.source_add_chatgpt_file(
            notebook_id="notebook-123",
            file={
                "download_url": "https://example.test/upload.txt",
                "file_id": "file_123",
                "file_name": "upload.txt",
            },
            title="Uploaded from ChatGPT",
            wait=True,
            cleanup=True,
        )

    assert result["status"] == "success"
    assert result["source_id"] == "source-123"
    assert result["chatgpt_file"]["original_file_id"] == "file_123"
    assert result["chatgpt_file"]["file_name"] == "upload.txt"
    assert result["chatgpt_file"]["size_bytes"] == len(b"hello from chatgpt")
    assert result["chatgpt_file"]["cached_path"] is None
    assert cached_paths
    assert not cached_paths[0].exists()
