"""Emit pip requirements from pyproject.toml dependencies.

Dockerfiles use this tiny helper to install third-party dependencies before
copying application source, which keeps the expensive dependency layer cached
when only service code changes.
"""

from __future__ import annotations

import argparse
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 fallback
    import tomli as tomllib


def collect_requirements(pyproject: Path, extras: list[str]) -> list[str]:
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    project = data.get("project", {})
    optional = project.get("optional-dependencies", {})
    seen: set[str] = set()
    out: list[str] = []

    def add(req: str) -> None:
        if req not in seen:
            seen.add(req)
            out.append(req)

    for req in project.get("dependencies", []):
        add(req)
    for extra in extras:
        for req in optional.get(extra, []):
            add(req)
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("pyproject", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--extra", action="append", default=[])
    args = parser.parse_args()

    requirements = collect_requirements(args.pyproject, args.extra)
    args.output.write_text("\n".join(requirements) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
