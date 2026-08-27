from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from project_management.cli import (  # noqa: E402
    ProjectManager,
    _import_completed,
    main as project_manager_main,
)
from project_management.deliverables import (  # noqa: E402
    load_manifest,
    register_deliverable,
)
from project_management.finalize import (  # noqa: E402
    _source_fingerprint,
    finalize_project,
)
import visual_review  # noqa: E402


class ViviCommunicationContractTests(unittest.TestCase):
    def test_role_switch_audit_stays_internal(self) -> None:
        skill = (Path(__file__).resolve().parents[1] / "SKILL.md").read_text(
            encoding="utf-8"
        )

        self.assertNotIn("## [Role Switch:", skill)
        self.assertIn("Role transitions", skill)
        self.assertIn("IDE-oriented Role Switch audit block", skill)
        self.assertIn("Do not narrate individual tool calls", skill)


class ProjectManagerImportTests(unittest.TestCase):
    def _project(self, root: Path) -> Path:
        project = root / "projects" / "demo"
        (project / "sources").mkdir(parents=True)
        (project / "analysis").mkdir()
        return project

    def test_runtime_projects_root_uses_host_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            configured = root / "host-projects"
            with mock.patch.dict(
                os.environ,
                {"PPT_MASTER_PROJECTS_ROOT": str(configured)},
            ):
                manager = ProjectManager()

            self.assertEqual(manager.base_dir, configured)

    def test_relative_project_and_source_become_absolute_before_tools_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            project = self._project(root)
            source = root / "sample.pptx"
            source.write_bytes(b"pptx fixture")
            manager = ProjectManager(root / "projects")
            observed: dict[str, Path] = {}

            def fake_intake(presentation: Path, project_dir: Path) -> Path:
                observed["intake_source"] = presentation
                observed["project"] = project_dir
                return project_dir / "analysis"

            def fake_convert(presentation: Path, markdown: Path) -> None:
                observed["convert_source"] = presentation
                observed["markdown"] = markdown
                markdown.write_text("# imported\n", encoding="utf-8")

            with contextlib.chdir(root):
                with mock.patch.object(
                    manager,
                    "_import_pptx_intake",
                    side_effect=fake_intake,
                ), mock.patch.object(
                    manager,
                    "_import_presentation",
                    side_effect=fake_convert,
                ):
                    summary = manager.import_sources(
                        "projects/demo",
                        ["sample.pptx"],
                        copy=True,
                    )

            self.assertEqual(summary["errors"], [])
            self.assertEqual(observed["project"], project)
            self.assertEqual(observed["intake_source"], project / "sources/sample.pptx")
            self.assertEqual(observed["convert_source"], project / "sources/sample.pptx")
            self.assertEqual(observed["markdown"], project / "sources/sample.md")
            self.assertTrue(all(path.is_absolute() for path in observed.values()))

    def test_reimporting_identical_file_reuses_the_archived_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            project = self._project(root)
            source = root / "source.bin"
            source.write_bytes(b"same content")
            manager = ProjectManager(root / "projects")

            first = manager.import_sources(str(project), [str(source)], copy=True)
            second = manager.import_sources(str(project), [str(source)], copy=True)

            archived = project / "sources/source.bin"
            self.assertEqual(first["archived"], [str(archived)])
            self.assertEqual(second["archived"], [str(archived)])
            self.assertFalse((project / "sources/source_2.bin").exists())

    def test_same_name_with_different_content_keeps_both_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            project = self._project(root)
            source = root / "source.bin"
            manager = ProjectManager(root / "projects")

            source.write_bytes(b"first")
            manager.import_sources(str(project), [str(source)], copy=True)
            source.write_bytes(b"second")
            second = manager.import_sources(str(project), [str(source)], copy=True)

            self.assertEqual(
                second["archived"],
                [str(project / "sources/source_2.bin")],
            )

    def test_required_conversion_failure_marks_the_import_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            project = self._project(root)
            source = root / "source.pdf"
            source.write_bytes(b"pdf fixture")
            manager = ProjectManager(root / "projects")

            with mock.patch.object(
                manager,
                "_import_pdf",
                side_effect=RuntimeError("converter failed"),
            ):
                summary = manager.import_sources(
                    str(project),
                    [str(source)],
                    copy=True,
                )

            self.assertTrue(summary["archived"])
            self.assertEqual(len(summary["errors"]), 1)
            self.assertIn(str(source), summary["errors"][0])
            self.assertFalse(_import_completed(summary))

    def test_import_sources_json_stdout_is_one_result_object(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            project = self._project(root)
            source = root / "source.bin"
            source.write_bytes(b"fixture")
            stdout = io.StringIO()

            with contextlib.redirect_stdout(stdout):
                result = project_manager_main(
                    [
                        "import-sources",
                        str(project),
                        str(source),
                        "--copy",
                        "--json",
                    ]
                )

            payload = json.loads(stdout.getvalue())
            self.assertEqual(result, 0)
            self.assertEqual(payload["schema"], "ppt-master.import-sources-result.v1")
            self.assertEqual(payload["status"], "completed")
            self.assertEqual(payload["summary"]["errors"], [])


class DeliverableManifestTests(unittest.TestCase):
    def test_registers_project_relative_markdown_with_integrity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project = Path(temp_dir).resolve() / "project"
            project.mkdir()
            document = project / "summary.md"
            document.write_text("# Summary\n", encoding="utf-8")

            entry, manifest_path = register_deliverable(
                project,
                "summary.md",
                required=True,
            )

            self.assertEqual(entry["path"], "summary.md")
            self.assertEqual(entry["media_type"], "text/markdown")
            self.assertEqual(len(entry["sha256"]), 64)
            self.assertEqual(manifest_path, project / "deliverables/manifest.json")
            self.assertEqual(load_manifest(project)["items"], [entry])
            self.assertNotIn(str(project), manifest_path.read_text(encoding="utf-8"))

    def test_rejects_a_deliverable_outside_the_project(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            project = root / "project"
            project.mkdir()
            outside = root / "outside.txt"
            outside.write_text("no", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "inside the project"):
                register_deliverable(project, outside)

    def test_rejects_a_symlinked_deliverables_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            project = root / "project"
            project.mkdir()
            outside = root / "outside"
            outside.mkdir()
            (project / "deliverables").symlink_to(outside, target_is_directory=True)
            document = project / "summary.md"
            document.write_text("# Summary\n", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "must not be a symlink"):
                register_deliverable(project, document)

    def test_rejects_project_internal_files_as_deliverables(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project = Path(temp_dir).resolve() / "project"
            source = project / "sources" / "uploaded.txt"
            source.parent.mkdir(parents=True)
            source.write_text("untrusted input", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "reserved for project internals"):
                register_deliverable(project, source)


class FinalizeProjectTests(unittest.TestCase):
    def _project(self, root: Path) -> Path:
        project = root / "project"
        (project / "svg_output").mkdir(parents=True)
        (project / "validation").mkdir()
        (project / "exports").mkdir()
        (project / "svg_output/01_cover.svg").write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1600 900"/>',
            encoding="utf-8",
        )
        return project

    def test_finalize_writes_result_checkpoint_and_delivery_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project = self._project(Path(temp_dir).resolve())
            commands: list[list[str]] = []

            def fake_runner(command: list[str], *, cwd: Path):
                commands.append(command)
                fingerprint = _source_fingerprint(project)
                if command[1].endswith("svg_quality_checker.py"):
                    (project / "validation/svg_quality_report.json").write_text(
                        json.dumps(
                            {
                                "schema": "ppt-master.svg-quality-report.v1",
                                "stage": "final",
                                "source_fingerprint": fingerprint,
                                "categories": {
                                    "blocking": {"count": 0},
                                    "introduced": {"count": 0},
                                },
                            }
                        ),
                        encoding="utf-8",
                    )
                    return subprocess.CompletedProcess(command, 0, "quality passed\n", "")
                output = project / "exports/demo.pptx"
                output.write_bytes(b"pptx")
                report = project / "validation/demo.report.json"
                report.write_text(
                    json.dumps(
                        {
                            "schema": "ppt-master.pptx-postflight-report.v1",
                            "status": "passed",
                            "source": {"fingerprint": fingerprint},
                        }
                    ),
                    encoding="utf-8",
                )
                stdout = f"  [PPTX] {output}\n  [REPORT] {report}\n"
                return subprocess.CompletedProcess(command, 0, stdout, "")

            result = finalize_project(project, runner=fake_runner)

            self.assertEqual(result["status"], "completed")
            self.assertEqual(len(commands), 2)
            self.assertEqual(result["export"]["path"], "exports/demo.pptx")
            self.assertEqual(
                load_manifest(project)["items"][0]["status"],
                "ready",
            )
            state = json.loads(
                (project / "validation/finalize_state.json").read_text(encoding="utf-8")
            )
            self.assertEqual(state["status"], "completed")

            def unexpected_runner(*_args, **_kwargs):
                raise AssertionError("completed current finalize must be reusable")

            reused = finalize_project(project, runner=unexpected_runner)
            self.assertEqual(reused["quality"]["status"], "reused")
            self.assertEqual(reused["export"]["status"], "reused")

    def test_quality_failure_is_persisted_and_stops_before_export(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project = self._project(Path(temp_dir).resolve())
            commands: list[list[str]] = []

            def failing_runner(command: list[str], *, cwd: Path):
                commands.append(command)
                return subprocess.CompletedProcess(command, 1, "", "blocking error")

            result = finalize_project(project, runner=failing_runner)

            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["phase"], "quality")
            self.assertEqual(len(commands), 1)
            state = json.loads(
                (project / "validation/finalize_state.json").read_text(encoding="utf-8")
            )
            self.assertEqual(state["status"], "failed")


class VisualReviewLifecycleTests(unittest.TestCase):
    def test_auto_started_preview_is_verified_and_stoppable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project = Path(temp_dir).resolve()
            completed = subprocess.CompletedProcess([], 0, "", "")
            with mock.patch.object(
                visual_review.subprocess,
                "run",
                return_value=completed,
            ) as run, mock.patch.object(
                visual_review,
                "discover_server_url",
                return_value="http://127.0.0.1:6060",
            ), mock.patch.object(visual_review, "check_server") as check:
                url = visual_review.start_project_server(project)
                stopped = visual_review.stop_project_server(project)

            self.assertEqual(url, "http://127.0.0.1:6060")
            self.assertTrue(stopped)
            check.assert_called_once_with(url, project)
            self.assertEqual(run.call_count, 2)

    def test_failed_preview_verification_stops_the_started_server(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project = Path(temp_dir).resolve()
            completed = subprocess.CompletedProcess([], 0, "", "")
            with mock.patch.object(
                visual_review.subprocess,
                "run",
                return_value=completed,
            ) as run, mock.patch.object(
                visual_review,
                "discover_server_url",
                return_value="http://127.0.0.1:6060",
            ), mock.patch.object(
                visual_review,
                "check_server",
                side_effect=RuntimeError("wrong project"),
            ):
                with self.assertRaisesRegex(RuntimeError, "wrong project"):
                    visual_review.start_project_server(project)

            self.assertEqual(run.call_count, 2)


if __name__ == "__main__":
    unittest.main()
