"""Project-scoped deliverable manifest helpers.

The Skill records publishable files here; the host decides how to expose them.
Manifest paths stay relative to the project so internal workspace locations do
not become part of the delivery contract.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path


MANIFEST_SCHEMA = "ppt-master.deliverables-manifest.v1"
MANIFEST_RELATIVE_PATH = Path("deliverables") / "manifest.json"
_MEDIA_TYPES = {
    ".md": "text/markdown",
    ".pdf": "application/pdf",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".txt": "text/plain",
}
_NON_DELIVERABLE_PARTS = {
    ".git",
    ".opencode",
    ".preview",
    "deliverables",
    "node_modules",
    "sources",
}


def utc_now() -> str:
    """Return one stable UTC timestamp for machine-readable state."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def write_json_atomic(path: Path, payload: object) -> None:
    """Write JSON through a same-directory temporary file and atomic rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise RuntimeError(f"Refusing to replace symlink: {path}")
    fd, temp_name = tempfile.mkstemp(
        prefix=f"{path.stem}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def file_sha256(path: Path) -> str:
    """Return the SHA-256 digest for one regular file."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _default_manifest() -> dict[str, object]:
    return {
        "schema": MANIFEST_SCHEMA,
        "updated_at": utc_now(),
        "items": [],
    }


def _manifest_path(project: Path) -> Path:
    directory = project / MANIFEST_RELATIVE_PATH.parent
    if directory.is_symlink():
        raise RuntimeError(f"Deliverable directory must not be a symlink: {directory}")
    resolved_directory = directory.resolve()
    try:
        resolved_directory.relative_to(project)
    except ValueError as exc:
        raise RuntimeError(
            f"Deliverable directory must stay inside the project: {resolved_directory}"
        ) from exc
    return directory / MANIFEST_RELATIVE_PATH.name


def load_manifest(project_path: str | Path) -> dict[str, object]:
    """Load and validate the project deliverable manifest."""
    project = Path(project_path).expanduser().resolve()
    path = _manifest_path(project)
    if not path.exists():
        return _default_manifest()
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"Deliverable manifest must be a regular file: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Deliverable manifest is unreadable: {path} ({exc})") from exc
    if not isinstance(payload, dict) or payload.get("schema") != MANIFEST_SCHEMA:
        raise RuntimeError(f"Unsupported deliverable manifest schema: {path}")
    items = payload.get("items")
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise RuntimeError(f"Deliverable manifest items must be an array of objects: {path}")
    return payload


def _resolve_project_file(project: Path, file_path: str | Path) -> tuple[Path, str]:
    raw = Path(file_path).expanduser()
    candidate = raw if raw.is_absolute() else project / raw
    if candidate.is_symlink():
        raise RuntimeError(f"Deliverable must not be a symlink: {candidate}")
    resolved = candidate.resolve()
    try:
        relative = resolved.relative_to(project)
    except ValueError as exc:
        raise RuntimeError(f"Deliverable must stay inside the project: {resolved}") from exc
    if not resolved.is_file():
        raise FileNotFoundError(f"Deliverable file not found: {resolved}")
    if any(
        part in _NON_DELIVERABLE_PARTS or part.startswith(".")
        for part in relative.parts
    ):
        raise RuntimeError(f"Path is reserved for project internals: {relative}")
    return resolved, relative.as_posix()


def register_deliverable(
    project_path: str | Path,
    file_path: str | Path,
    *,
    role: str = "supplementary",
    required: bool = False,
    source: str = "manual",
    supersede_role: bool = False,
) -> tuple[dict[str, object], Path]:
    """Upsert one verified project file and return its entry and manifest path."""
    if role not in {"primary", "supplementary", "supporting"}:
        raise ValueError(f"Unsupported deliverable role: {role}")
    project = Path(project_path).expanduser().resolve()
    if not project.is_dir():
        raise FileNotFoundError(f"Project directory not found: {project}")
    resolved, relative = _resolve_project_file(project, file_path)
    manifest = load_manifest(project)
    items = list(manifest["items"])
    now = utc_now()

    if supersede_role:
        for item in items:
            if item.get("role") == role and item.get("status") == "ready":
                item["status"] = "superseded"
                item["superseded_at"] = now

    suffix = resolved.suffix.lower()
    media_type = _MEDIA_TYPES.get(suffix) or mimetypes.guess_type(resolved.name)[0]
    media_type = media_type or "application/octet-stream"
    entry: dict[str, object] = {
        "id": hashlib.sha256(relative.encode("utf-8")).hexdigest()[:20],
        "path": relative,
        "name": resolved.name,
        "media_type": media_type,
        "bytes": resolved.stat().st_size,
        "sha256": file_sha256(resolved),
        "role": role,
        "required": bool(required),
        "status": "ready",
        "source": source,
        "registered_at": now,
    }
    items = [item for item in items if item.get("path") != relative]
    items.append(entry)
    manifest["updated_at"] = now
    manifest["items"] = items
    manifest_path = _manifest_path(project)
    write_json_atomic(manifest_path, manifest)
    return entry, manifest_path


def ready_entry_for_path(
    project_path: str | Path,
    file_path: str | Path,
) -> dict[str, object] | None:
    """Return the ready entry when path, size, and digest still match."""
    project = Path(project_path).expanduser().resolve()
    try:
        resolved, relative = _resolve_project_file(project, file_path)
        manifest = load_manifest(project)
    except (FileNotFoundError, RuntimeError):
        return None
    for item in manifest["items"]:
        if item.get("path") != relative or item.get("status") != "ready":
            continue
        if item.get("bytes") != resolved.stat().st_size:
            return None
        if item.get("sha256") != file_sha256(resolved):
            return None
        return item
    return None
