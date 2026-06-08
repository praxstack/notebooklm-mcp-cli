"""Source tools - Source management with consolidated source_add."""

import hashlib
import json
import os
import tempfile
import time
import urllib.request
from pathlib import Path
from urllib.parse import quote, urlparse

from ...services import ServiceError, ValidationError
from ...services import sources as sources_service
from ._utils import (
    PUBLIC_DIR,
    ResultDict,
    coerce_list,
    error_result,
    get_client,
    get_mcp_base_url,
    logged_tool,
)

CHATGPT_FILE_MAX_BYTES = int(
    os.environ.get("NOTEBOOKLM_CHATGPT_FILE_MAX_BYTES", str(25 * 1024 * 1024))
)
CHATGPT_FILE_CACHE_DIR = Path(
    os.environ.get("NOTEBOOKLM_CHATGPT_FILE_CACHE_DIR", "")
    or Path(tempfile.gettempdir()) / "notebooklm-chatgpt-files"
)
CHATGPT_FILE_ALLOWED_EXTENSIONS = {
    ".pdf",
    ".txt",
    ".md",
    ".docx",
    ".csv",
    ".epub",
    ".mp3",
    ".m4a",
    ".wav",
    ".aac",
    ".ogg",
    ".opus",
    ".mp4",
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
}


def _safe_chatgpt_filename(name: object, default: str = "chatgpt-upload.bin") -> str:
    raw = Path(str(name or default)).name
    safe = "".join(ch if ch.isalnum() or ch in "._- ()" else "_" for ch in raw).strip(". ")
    return safe[:180] or default


def _coerce_chatgpt_file_reference(file: dict[str, object] | str | None) -> dict[str, object]:
    if isinstance(file, dict):
        return file
    if isinstance(file, str):
        raw = file.strip()
        if not raw:
            raise ValidationError(
                "file must be a resolved ChatGPT file object, not an empty string."
            )
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValidationError(
                "file must be a resolved ChatGPT file object or a JSON-encoded file object."
            ) from exc
        if isinstance(parsed, dict):
            return parsed
    raise ValidationError("file must be a ChatGPT file object.")


def _chatgpt_file_url(file: dict[str, object]) -> str:
    value = file.get("download_url") or file.get("download_link")
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("file.download_url is required for ChatGPT file uploads.")
    parsed = urlparse(value)
    if parsed.scheme.lower() != "https":
        raise ValidationError("ChatGPT file download URL must use https.")
    return value


def _download_chatgpt_file(file: dict[str, object]) -> dict[str, object]:
    file_name = _safe_chatgpt_filename(file.get("file_name") or file.get("name"))
    extension = Path(file_name).suffix.lower()
    if extension not in CHATGPT_FILE_ALLOWED_EXTENSIONS:
        raise ValidationError(
            f"Unsupported ChatGPT file extension '{extension or '[none]'}'. "
            "Upload a NotebookLM-supported file type."
        )

    CHATGPT_FILE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    local_path = CHATGPT_FILE_CACHE_DIR / file_name
    if local_path.exists():
        stem = local_path.stem[:120]
        local_path = CHATGPT_FILE_CACHE_DIR / f"{stem}-{os.urandom(4).hex()}{extension}"

    url = _chatgpt_file_url(file)
    request = urllib.request.Request(url, headers={"User-Agent": "notebooklm-mcp-chatgpt-file/1.0"})
    sha256 = hashlib.sha256()
    total = 0

    try:
        with urllib.request.urlopen(request, timeout=30) as response, local_path.open("xb") as out:
            declared = response.headers.get("Content-Length")
            if declared and int(declared) > CHATGPT_FILE_MAX_BYTES:
                raise ValidationError(
                    f"ChatGPT file exceeds size limit ({CHATGPT_FILE_MAX_BYTES} bytes)."
                )
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > CHATGPT_FILE_MAX_BYTES:
                    raise ValidationError(
                        f"ChatGPT file exceeds size limit ({CHATGPT_FILE_MAX_BYTES} bytes)."
                    )
                sha256.update(chunk)
                out.write(chunk)
    except Exception:
        local_path.unlink(missing_ok=True)
        raise

    if total == 0:
        local_path.unlink(missing_ok=True)
        raise ValidationError("ChatGPT file is empty.")

    return {
        "file_path": str(local_path),
        "file_name": file_name,
        "size_bytes": total,
        "sha256": sha256.hexdigest(),
        "original_file_id": str(file.get("file_id") or file.get("id") or ""),
    }


def _normalize_source_validation_error(message: str) -> str:
    """Preserve historical MCP wire wording for invalid source_type."""
    if message.startswith("Unknown source type "):
        return message.replace("Unknown source type", "Unknown source_type", 1)
    return message


@logged_tool(meta={"openai/fileParams": ["file"]})
def source_add_chatgpt_file(
    notebook_id: str,
    file: dict[str, object] | str | None,
    title: str | None = None,
    wait: bool = False,
    wait_timeout: float = 120.0,
    cleanup: bool = True,
) -> ResultDict:
    """Add a normally uploaded ChatGPT file to a NotebookLM notebook.

    ChatGPT Apps SDK resolves the top-level `file` parameter when this tool is
    called with a user-uploaded file. The resolved object contains a temporary
    HTTPS download URL plus file metadata. This tool downloads the file into a
    local cache, uploads it to NotebookLM through the existing local-file source
    path, then optionally deletes the cached copy.
    """
    cached: dict[str, object] | None = None
    try:
        file_ref = _coerce_chatgpt_file_reference(file)
        cached = _download_chatgpt_file(file_ref)
        result = source_add(
            notebook_id=notebook_id,
            source_type="file",
            file_path=str(cached["file_path"]),
            title=title,
            wait=wait,
            wait_timeout=wait_timeout,
        )
        if result.get("status") == "success":
            result["chatgpt_file"] = {
                "original_file_id": cached["original_file_id"],
                "file_name": cached["file_name"],
                "size_bytes": cached["size_bytes"],
                "sha256": cached["sha256"],
                "cached_path": None if cleanup else cached["file_path"],
                "cleanup": cleanup,
            }
        return result
    except ValidationError as e:
        return error_result(_normalize_source_validation_error(str(e)))
    except ServiceError as e:
        return error_result(e.user_message, hint=e.hint)
    except Exception as e:
        return error_result(str(e))
    finally:
        if cleanup and cached and cached.get("file_path"):
            Path(str(cached["file_path"])).unlink(missing_ok=True)


@logged_tool()
def source_add(
    notebook_id: str,
    source_type: str,
    url: str | None = None,
    urls: list[str] | None = None,
    text: str | None = None,
    title: str | None = None,
    file_path: str | None = None,
    document_id: str | None = None,
    doc_type: str = "doc",
    wait: bool = False,
    wait_timeout: float = 120.0,
) -> ResultDict:
    """Add a source to a notebook. Unified tool for all source types.

    Supports: url, text, drive, file

    Args:
        notebook_id: Notebook UUID
        source_type: Type of source to add:
            - url: Web page or YouTube URL
            - text: Pasted text content
            - drive: Google Drive document
            - file: Local file upload. Supported extensions:
                PDF, TXT, MD, DOCX, CSV, EPUB, MP3, M4A, WAV, AAC, OGG,
                OPUS, MP4, JPG, JPEG, PNG, GIF, WEBP. Image-bearing
                sources (PDF / JPG / PNG / etc.) feed Studio video
                generation's visual-crop pipeline Ã¢â‚¬â€ charts, photos, and
                diagrams may be extracted as on-screen aids in Video
                Overviews.
        url: URL to add (for source_type=url)
        urls: List of URLs to add in bulk (for source_type=url, alternative to url)
        text: Text content to add (for source_type=text)
        title: Display title (for text sources)
        file_path: Local file path (for source_type=file)
        document_id: Google Drive document ID (for source_type=drive)
        doc_type: Drive doc type: doc|slides|sheets|pdf (for source_type=drive)
        wait: If True, wait for source processing to complete before returning
        wait_timeout: Max seconds to wait if wait=True (default 120)

    Example:
        source_add(notebook_id="abc", source_type="url", url="https://example.com")
        source_add(notebook_id="abc", source_type="url", urls=["https://a.com", "https://b.com"])
        source_add(notebook_id="abc", source_type="url", url="https://example.com", wait=True)
        source_add(notebook_id="abc", source_type="file", file_path="/path/to/doc.pdf", wait=True)
        source_add(notebook_id="abc", source_type="file", file_path="/path/to/screenshot.png", wait=True)
    """
    try:
        client = get_client()

        # Coerce list params from MCP clients (may arrive as strings)
        coerced_urls: list[str] | None = coerce_list(urls)

        # Bulk URL add: when urls list is provided
        if coerced_urls and source_type == "url":
            bulk_result = sources_service.add_sources(
                client,
                notebook_id,
                [{"source_type": "url", "url": url_value} for url_value in coerced_urls],
                wait=wait,
                wait_timeout=wait_timeout,
            )
            return {"status": "success", "ready": wait, **bulk_result}

        # Single source add (existing behavior)
        single_result = sources_service.add_source(
            client,
            notebook_id,
            source_type,
            url=url,
            text=text,
            title=title,
            file_path=file_path,
            document_id=document_id,
            doc_type=doc_type,
            wait=wait,
            wait_timeout=wait_timeout,
        )
        return {"status": "success", "ready": wait, **single_result}
    except ValidationError as e:
        return error_result(_normalize_source_validation_error(str(e)))
    except ServiceError as e:
        return error_result(e.user_message, hint=e.hint)
    except Exception as e:
        return error_result(str(e))


@logged_tool()
def source_list_drive(notebook_id: str) -> ResultDict:
    """List sources with types and Drive freshness status.

    Use before source_sync_drive to identify stale sources.

    Args:
        notebook_id: Notebook UUID
    """
    try:
        client = get_client()
        result = sources_service.list_drive_sources(client, notebook_id)
        return {"status": "success", "notebook_id": notebook_id, **result}
    except ServiceError as e:
        return error_result(e.user_message, hint=e.hint)
    except Exception as e:
        return error_result(str(e))


@logged_tool()
def source_sync_drive(source_ids: list[str], confirm: bool = False) -> ResultDict:
    """Sync Drive sources with latest content. Requires confirm=True.

    Call source_list_drive first to identify stale sources.

    Args:
        source_ids: Source UUIDs to sync
        confirm: Must be True after user approval
    """
    if not confirm:
        return error_result(
            "Sync not confirmed. Set confirm=True after user approval.",
            hint="Call source_list_drive first to see which sources are stale.",
        )

    try:
        client = get_client()
        # Coerce list params from MCP clients (may arrive as strings)
        coerced_source_ids: list[str] | None = coerce_list(source_ids)
        if not coerced_source_ids:
            return error_result("source_ids is required.")
        sync_results = sources_service.sync_drive_sources(client, coerced_source_ids)
        synced_count = sum(1 for item in sync_results if item.get("synced"))
        return {
            "status": "success",
            "synced_count": synced_count,
            "total_count": len(coerced_source_ids),
            "results": sync_results,
        }
    except ServiceError as e:
        return error_result(e.user_message, hint=e.hint)
    except Exception as e:
        return error_result(str(e))


@logged_tool()
def source_rename(notebook_id: str, source_id: str, new_title: str) -> ResultDict:
    """Rename a source in a notebook.

    Args:
        notebook_id: Notebook UUID containing the source
        source_id: Source UUID to rename
        new_title: New display title for the source
    """
    try:
        client = get_client()
        result = sources_service.rename_source(client, notebook_id, source_id, new_title)
        return {"status": "success", **result}
    except ServiceError as e:
        return error_result(e.user_message, hint=e.hint)
    except Exception as e:
        return error_result(str(e))


@logged_tool()
def source_delete(
    source_id: str | None = None,
    source_ids: list[str] | None = None,
    confirm: bool = False,
) -> ResultDict:
    """Delete source(s) permanently. IRREVERSIBLE. Requires confirm=True.

    Args:
        source_id: Source UUID to delete (single)
        source_ids: List of source UUIDs to delete (bulk, alternative to source_id)
        confirm: Must be True after user approval
    """
    if not confirm:
        return error_result(
            "Deletion not confirmed. Set confirm=True after user approval.",
            warning="This action is IRREVERSIBLE.",
        )

    try:
        client = get_client()

        # Coerce list params from MCP clients (may arrive as strings)
        coerced_source_ids: list[str] | None = coerce_list(source_ids)

        # Bulk delete: when source_ids list is provided
        if coerced_source_ids:
            sources_service.delete_sources(client, coerced_source_ids)
            return {
                "status": "success",
                "message": f"{len(coerced_source_ids)} sources have been permanently deleted.",
                "deleted_count": len(coerced_source_ids),
            }

        # Single delete (existing behavior)
        if not source_id:
            return error_result("Either source_id or source_ids is required.")

        sources_service.delete_source(client, source_id)
        return {
            "status": "success",
            "message": f"Source {source_id} has been permanently deleted.",
        }
    except ServiceError as e:
        return error_result(e.user_message, hint=e.hint)
    except Exception as e:
        return error_result(str(e))


@logged_tool()
def source_describe(source_id: str) -> ResultDict:
    """Get AI-generated source summary with keyword chips.

    Args:
        source_id: Source UUID

    Returns: summary (markdown with **bold** keywords), keywords list
    """
    try:
        client = get_client()
        result = sources_service.describe_source(client, source_id)
        return {"status": "success", **result}
    except ServiceError as e:
        return error_result(e.user_message, hint=e.hint)
    except Exception as e:
        return error_result(str(e))


def _source_content_transient(message: str) -> bool:
    """Return True for NotebookLM source-content states that may resolve after polling."""
    lowered = message.lower()
    return any(
        phrase in lowered
        for phrase in (
            "failed to get source content",
            "no content returned",
            "not ready",
            "processing",
            "indexing",
            "try again",
        )
    )


@logged_tool()
def source_get_content(
    source_id: str,
    wait: bool = True,
    wait_timeout: float = 120.0,
    poll_interval: float = 3.0,
) -> ResultDict:
    """Get raw text content of a source (no AI processing).

    Returns the original indexed text from PDFs, web pages, pasted text,
    or YouTube transcripts. Much faster than notebook_query for content export.

    Args:
        source_id: Source UUID
        wait: If True, poll while NotebookLM is still indexing the source.
        wait_timeout: Maximum seconds to wait when wait=True.
        poll_interval: Seconds between readiness checks.

    Returns: content (str), title (str), source_type (str), char_count (int), download_url (str when available)
    """
    try:
        client = get_client()
        deadline = time.monotonic() + max(0.0, wait_timeout)
        attempts = 0
        last_error: ServiceError | None = None

        while True:
            attempts += 1
            try:
                result = sources_service.get_source_content(client, source_id)
                if result.get("content") or not wait:
                    break
            except ServiceError as e:
                last_error = e
                message = f"{e.user_message} {e}"
                if (
                    not wait
                    or not _source_content_transient(message)
                    or time.monotonic() >= deadline
                ):
                    raise
            if not wait or time.monotonic() >= deadline:
                if last_error:
                    raise last_error
                return error_result(
                    "Source content is not ready yet.",
                    status="pending",
                    hint="NotebookLM may still be indexing this source. Retry source_get_content shortly or increase wait_timeout.",
                    source_id=source_id,
                    attempts=attempts,
                )
            time.sleep(max(0.5, poll_interval))

        try:
            content = result.get("content", "")
            title = result.get("title", "source")
            safe_title = _safe_chatgpt_filename(title, default="source")
            if not safe_title.endswith(".txt") and not safe_title.endswith(".md"):
                safe_title += ".txt"

            PUBLIC_DIR.mkdir(parents=True, exist_ok=True)
            pub_path = PUBLIC_DIR / safe_title
            if pub_path.exists():
                pub_path = (
                    PUBLIC_DIR
                    / f"{Path(safe_title).stem}-{os.urandom(2).hex()}{Path(safe_title).suffix}"
                )

            pub_path.write_text(content, encoding="utf-8")

            base_url = get_mcp_base_url()
            if base_url:
                result["download_url"] = f"{base_url}/artifacts/{quote(pub_path.name)}"
        except Exception as e:
            import logging

            logging.getLogger("notebooklm_tools.mcp").warning(
                f"Failed to copy public source file: {e}"
            )

        return {"status": "success", **result}

    except ServiceError as e:
        return error_result(e.user_message, hint=e.hint)
    except Exception as e:
        return error_result(str(e))
