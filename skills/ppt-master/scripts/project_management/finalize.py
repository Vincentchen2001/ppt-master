"""Resumable project quality gate, export, and delivery registration."""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Callable

from slide_roster import discover_slide_svgs

from .deliverables import (
    MANIFEST_RELATIVE_PATH,
    ready_entry_for_path,
    register_deliverable,
    utc_now,
    write_json_atomic,
)
from .paths import SCRIPTS_DIR


STATE_SCHEMA = "ppt-master.finalize-state.v1"
RESULT_SCHEMA = "ppt-master.finalize-result.v1"
STATE_RELATIVE_PATH = Path("validation") / "finalize_state.json"
RESULT_RELATIVE_PATH = Path("validation") / "finalize_result.json"
LOG_RELATIVE_PATH = Path("validation") / "finalize_last.log"
_PPTX_LINE_RE = re.compile(r"^\s*\[PPTX\]\s+(.+?)\s*$", re.MULTILINE)
_REPORT_LINE_RE = re.compile(r"^\s*\[REPORT\]\s+(.+?)\s*$", re.MULTILINE)


def _project_relative(project: Path, path: Path) -> str:
    resolved = path.expanduser().resolve()
    try:
        return resolved.relative_to(project).as_posix()
    except ValueError as exc:
        raise RuntimeError(f"Finalized artifact must stay inside the project: {resolved}") from exc


def _source_fingerprint(project: Path) -> dict[str, object]:
    svg_dir = project / "svg_output"
    files = discover_slide_svgs(svg_dir) if svg_dir.is_dir() else []
    if not files:
        raise RuntimeError(f"No authored SVG slides found in: {svg_dir}")
    rows: list[dict[str, object]] = []
    aggregate = hashlib.sha256()
    for path in sorted(files, key=lambda item: item.name):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        rows.append({"file": path.name, "sha256": digest})
        aggregate.update(path.name.encode("utf-8"))
        aggregate.update(b"\0")
        aggregate.update(digest.encode("ascii"))
        aggregate.update(b"\n")
    return {
        "algorithm": "sha256",
        "digest": aggregate.hexdigest(),
        "file_count": len(rows),
        "files": rows,
    }


def _fingerprints_match(left: object, right: object) -> bool:
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    return (
        left.get("algorithm") == right.get("algorithm") == "sha256"
        and left.get("digest") == right.get("digest")
        and left.get("file_count") == right.get("file_count")
    )


def _read_json_object(path: Path) -> dict[str, object] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _quality_report_passes(project: Path, fingerprint: dict[str, object]) -> bool:
    report = _read_json_object(project / "validation" / "svg_quality_report.json")
    if not report or report.get("schema") != "ppt-master.svg-quality-report.v1":
        return False
    if report.get("stage") != "final":
        return False
    if not _fingerprints_match(report.get("source_fingerprint"), fingerprint):
        return False
    categories = report.get("categories")
    if not isinstance(categories, dict):
        return False
    blocking = categories.get("blocking")
    return isinstance(blocking, dict) and blocking.get("count") == 0


def _load_state(project: Path) -> dict[str, object] | None:
    state = _read_json_object(project / STATE_RELATIVE_PATH)
    if state and state.get("schema") == STATE_SCHEMA:
        return state
    return None


def _resumable_output(
    project: Path,
    state: dict[str, object] | None,
    fingerprint: dict[str, object],
) -> tuple[Path, Path] | None:
    if not state:
        return None
    output = state.get("output")
    if not isinstance(output, dict):
        return None
    output_rel = output.get("path")
    report_rel = output.get("report")
    if not isinstance(output_rel, str) or not isinstance(report_rel, str):
        return None
    output_path = (project / output_rel).resolve()
    report_path = (project / report_rel).resolve()
    try:
        output_path.relative_to(project)
        report_path.relative_to(project)
    except ValueError:
        return None
    if not output_path.is_file() or not report_path.is_file():
        return None
    report = _read_json_object(report_path)
    if not report or report.get("schema") != "ppt-master.pptx-postflight-report.v1":
        return None
    if report.get("status") not in {"passed", "passed-with-warnings"}:
        return None
    source = report.get("source")
    if not isinstance(source, dict) or not _fingerprints_match(
        source.get("fingerprint"), fingerprint
    ):
        return None
    return output_path, report_path


def _run_command(
    command: list[str],
    *,
    cwd: Path,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _append_command_log(path: Path, command: list[str], result: subprocess.CompletedProcess[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"$ {shlex.join(command)}\n")
        if result.stdout:
            handle.write(result.stdout)
            if not result.stdout.endswith("\n"):
                handle.write("\n")
        if result.stderr:
            handle.write("[stderr]\n")
            handle.write(result.stderr)
            if not result.stderr.endswith("\n"):
                handle.write("\n")
        handle.write(f"[exit] {result.returncode}\n\n")


def _failure_detail(result: subprocess.CompletedProcess[str]) -> str:
    detail = (result.stderr or result.stdout or "command failed").strip()
    return detail[-4000:]


def _write_failure(
    project: Path,
    state: dict[str, object],
    *,
    phase: str,
    error: str,
) -> dict[str, object]:
    state["status"] = "failed"
    state["phase"] = phase
    state["updated_at"] = utc_now()
    state["error"] = error
    write_json_atomic(project / STATE_RELATIVE_PATH, state)
    result: dict[str, object] = {
        "schema": RESULT_SCHEMA,
        "status": "failed",
        "phase": phase,
        "error": error,
        "state": STATE_RELATIVE_PATH.as_posix(),
        "log": LOG_RELATIVE_PATH.as_posix(),
    }
    write_json_atomic(project / RESULT_RELATIVE_PATH, result)
    return result


def _export_snapshot(project: Path) -> dict[Path, tuple[int, int]]:
    exports = project / "exports"
    if not exports.is_dir():
        return {}
    return {
        path.resolve(): (path.stat().st_mtime_ns, path.stat().st_size)
        for path in exports.glob("*.pptx")
        if path.is_file()
    }


def _reported_path(pattern: re.Pattern[str], stdout: str, project: Path) -> Path | None:
    match = pattern.search(stdout)
    if not match:
        return None
    path = Path(match.group(1)).expanduser()
    return (path if path.is_absolute() else project / path).resolve()


def _find_export_output(
    project: Path,
    before: dict[Path, tuple[int, int]],
    stdout: str,
) -> Path | None:
    reported = _reported_path(_PPTX_LINE_RE, stdout, project)
    if reported and reported.is_file():
        return reported
    after = _export_snapshot(project)
    changed = [
        path
        for path, stat in after.items()
        if path not in before or before[path] != stat
    ]
    return max(changed, key=lambda path: path.stat().st_mtime_ns) if changed else None


def finalize_project(
    project_path: str | Path,
    *,
    quick_generate: bool = False,
    export_args: list[str] | None = None,
    extra_deliverables: list[str] | None = None,
    force: bool = False,
    runner: Callable[..., subprocess.CompletedProcess[str]] = _run_command,
) -> dict[str, object]:
    """Finalize one project and always persist a machine-readable result."""
    project = Path(project_path).expanduser().resolve()
    if not project.is_dir():
        raise FileNotFoundError(f"Project directory not found: {project}")
    fingerprint = _source_fingerprint(project)
    previous_state = _load_state(project)
    resumable = _resumable_output(project, previous_state, fingerprint)
    log_path = project / LOG_RELATIVE_PATH
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("", encoding="utf-8")
    now = utc_now()
    state: dict[str, object] = {
        "schema": STATE_SCHEMA,
        "status": "in-progress",
        "phase": "quality",
        "started_at": now,
        "updated_at": now,
        "source_fingerprint": fingerprint,
        "profile": "quick" if quick_generate else "default",
        "phases": {},
    }
    write_json_atomic(project / STATE_RELATIVE_PATH, state)
    phases = state["phases"]
    assert isinstance(phases, dict)

    quality_path = project / "validation" / "svg_quality_report.json"
    if _quality_report_passes(project, fingerprint):
        phases["quality"] = {
            "status": "reused",
            "report": _project_relative(project, quality_path),
        }
    else:
        quality_command = [
            sys.executable,
            str(SCRIPTS_DIR / "svg_quality_checker.py"),
            str(project),
            "--stage",
            "final",
            "--json",
        ]
        if quick_generate:
            quality_command.append("--quick-generate")
        quality_result = runner(quality_command, cwd=project)
        _append_command_log(log_path, quality_command, quality_result)
        if quality_result.returncode != 0 or not _quality_report_passes(project, fingerprint):
            phases["quality"] = {"status": "failed"}
            return _write_failure(
                project,
                state,
                phase="quality",
                error=_failure_detail(quality_result),
            )
        phases["quality"] = {
            "status": "passed",
            "report": _project_relative(project, quality_path),
        }

    state["phase"] = "export"
    state["updated_at"] = utc_now()
    write_json_atomic(project / STATE_RELATIVE_PATH, state)
    output_path: Path
    report_path: Path
    if resumable is not None and not force:
        output_path, report_path = resumable
        phases["export"] = {"status": "reused"}
    else:
        before = _export_snapshot(project)
        export_command = [
            sys.executable,
            str(SCRIPTS_DIR / "svg_to_pptx.py"),
            str(project),
        ]
        if quick_generate:
            export_command.append("--quick-generate")
        export_command.extend(export_args or [])
        export_result = runner(export_command, cwd=project)
        _append_command_log(log_path, export_command, export_result)
        if export_result.returncode != 0:
            phases["export"] = {"status": "failed"}
            return _write_failure(
                project,
                state,
                phase="export",
                error=_failure_detail(export_result),
            )
        found_output = _find_export_output(project, before, export_result.stdout or "")
        if found_output is None:
            return _write_failure(
                project,
                state,
                phase="export",
                error="Exporter returned success without a new or reported PPTX",
            )
        output_path = found_output
        reported = _reported_path(_REPORT_LINE_RE, export_result.stdout or "", project)
        report_path = reported or project / "validation" / f"{output_path.stem}.report.json"
        if _resumable_output(
            project,
            {
                "schema": STATE_SCHEMA,
                "output": {
                    "path": _project_relative(project, output_path),
                    "report": _project_relative(project, report_path),
                },
            },
            fingerprint,
        ) is None:
            return _write_failure(
                project,
                state,
                phase="export",
                error=f"Export postflight report is missing, failed, or stale: {report_path}",
            )
        phases["export"] = {"status": "passed"}

    output_rel = _project_relative(project, output_path)
    report_rel = _project_relative(project, report_path)
    state["output"] = {"path": output_rel, "report": report_rel}
    state["phase"] = "manifest"
    state["updated_at"] = utc_now()
    write_json_atomic(project / STATE_RELATIVE_PATH, state)

    try:
        primary, manifest_path = register_deliverable(
            project,
            output_rel,
            role="primary",
            required=True,
            source="finalize",
            supersede_role=True,
        )
        supplementary: list[dict[str, object]] = []
        for item in extra_deliverables or []:
            entry, _ = register_deliverable(
                project,
                item,
                role="supplementary",
                required=True,
                source="finalize",
            )
            supplementary.append(entry)
    except Exception as exc:
        phases["manifest"] = {"status": "failed"}
        return _write_failure(project, state, phase="manifest", error=str(exc))

    phases["manifest"] = {
        "status": "passed",
        "path": _project_relative(project, manifest_path),
    }
    state["status"] = "completed"
    state["phase"] = "completed"
    state["updated_at"] = utc_now()
    state.pop("error", None)
    write_json_atomic(project / STATE_RELATIVE_PATH, state)
    result: dict[str, object] = {
        "schema": RESULT_SCHEMA,
        "status": "completed",
        "profile": state["profile"],
        "quality": phases["quality"],
        "export": {
            "status": phases["export"]["status"],
            "path": output_rel,
            "report": report_rel,
        },
        "deliverables": {
            "manifest": MANIFEST_RELATIVE_PATH.as_posix(),
            "primary": primary,
            "supplementary": supplementary,
        },
        "state": STATE_RELATIVE_PATH.as_posix(),
        "log": LOG_RELATIVE_PATH.as_posix(),
    }
    write_json_atomic(project / RESULT_RELATIVE_PATH, result)
    return result


def completed_result_is_reusable(project_path: str | Path) -> bool:
    """Return whether the last completed primary output remains publishable."""
    project = Path(project_path).expanduser().resolve()
    try:
        fingerprint = _source_fingerprint(project)
    except (OSError, RuntimeError):
        return False
    state = _load_state(project)
    if not state or state.get("status") != "completed":
        return False
    resumable = _resumable_output(project, state, fingerprint)
    if resumable is None:
        return False
    output_path, _ = resumable
    return ready_entry_for_path(project, output_path) is not None
