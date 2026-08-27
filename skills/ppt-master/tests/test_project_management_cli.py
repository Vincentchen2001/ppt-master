from __future__ import annotations

import contextlib
import os
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
)


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


if __name__ == "__main__":
    unittest.main()
