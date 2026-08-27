#!/usr/bin/env python3
"""
PPT Master - Project Management Paths

Own the repository and Skill resource roots used by project-management modules.

Usage:
    Import the required path constants from project_management.paths.

Examples:
    from project_management.paths import PROJECTS_ROOT, SCHEMA_DIR

Dependencies:
    None (only uses the standard library)
"""

import os
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = PACKAGE_DIR.parent
SKILL_DIR = SCRIPTS_DIR.parent
REPO_ROOT = SKILL_DIR.parent.parent
PROJECTS_ROOT_ENV = "PPT_MASTER_PROJECTS_ROOT"


def resolve_projects_root(
    value: str | Path | None = None,
    *,
    cwd: Path | None = None,
) -> Path:
    """Resolve the host-owned projects root without relying on the Skill repo."""
    configured = value
    if configured is None:
        configured = os.environ.get(PROJECTS_ROOT_ENV)
    if configured is None:
        configured = (cwd or Path.cwd()) / "projects"
    return Path(configured).expanduser().resolve()


# Compatibility export for callers that import the historical constant. New
# project-management code resolves the value when ProjectManager is created so
# a host can inject the root through PPT_MASTER_PROJECTS_ROOT at runtime.
PROJECTS_ROOT = resolve_projects_root()
SOURCE_TO_MD_DIR = SCRIPTS_DIR / "source_to_md"
CHARTS_DIR = SKILL_DIR / "templates" / "charts"
SCHEMA_DIR = SKILL_DIR / "templates" / "schemas"
SCAFFOLD_DIR = SKILL_DIR / "templates" / "scaffolds"
