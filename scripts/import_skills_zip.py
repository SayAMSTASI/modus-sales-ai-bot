from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import stat
import zipfile
from pathlib import Path, PurePosixPath

ALLOWED_SUFFIXES = {".md", ".yaml", ".yml"}
SECRET_PATTERN = re.compile(
    r"(sk-[A-Za-z0-9_-]{20,}|[0-9]{8,}:[A-Za-z0-9_-]{20,}|"
    r"(?:api[_ -]?key|client[_ -]?secret)\s*[:=]\s*[^<{\s])",
    re.IGNORECASE,
)
MAX_FILES = 200
MAX_TOTAL_BYTES = 5_000_000


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _validated_files(archive: zipfile.ZipFile) -> tuple[list[zipfile.ZipInfo], set[str]]:
    files: list[zipfile.ZipInfo] = []
    roots: set[str] = set()
    total_bytes = 0
    names = {PurePosixPath(item.filename.replace("\\", "/")) for item in archive.infolist()}

    for info in archive.infolist():
        path = PurePosixPath(info.filename.replace("\\", "/"))
        if path.is_absolute() or ".." in path.parts or not path.parts:
            raise ValueError(f"Unsafe ZIP path: {info.filename}")
        if len(path.parts) < 2:
            if info.is_dir():
                continue
            raise ValueError(f"Every file must be inside a skill directory: {info.filename}")
        roots.add(path.parts[0])
        mode = info.external_attr >> 16
        if mode and stat.S_ISLNK(mode):
            raise ValueError(f"Symbolic links are not allowed: {info.filename}")
        if info.is_dir():
            continue
        if path.suffix.lower() not in ALLOWED_SUFFIXES:
            raise ValueError(f"Unsupported skill file type: {info.filename}")
        files.append(info)
        total_bytes += info.file_size

    if len(files) > MAX_FILES or total_bytes > MAX_TOTAL_BYTES:
        raise ValueError("Skill archive exceeds the safe size limit")
    for root in roots:
        if PurePosixPath(root, "SKILL.md") not in names:
            raise ValueError(f"Missing {root}/SKILL.md")
    return files, roots


def import_skills_zip(zip_path: Path, target: Path, *, replace: bool = False) -> list[Path]:
    with zipfile.ZipFile(zip_path) as archive:
        files, roots = _validated_files(archive)
        collisions = sorted(root for root in roots if (target / root).exists())
        if collisions and not replace:
            raise ValueError(
                "Skill directories already exist; use --replace explicitly: "
                + ", ".join(collisions)
            )

        contents: dict[PurePosixPath, bytes] = {}
        for info in files:
            path = PurePosixPath(info.filename.replace("\\", "/"))
            data = archive.read(info)
            text = data.decode("utf-8")
            if SECRET_PATTERN.search(text):
                raise ValueError(f"Secret-like value found in {info.filename}")
            contents[path] = data

    target.mkdir(parents=True, exist_ok=True)
    if replace:
        for root in roots:
            destination = (target / root).resolve()
            if destination.exists() and destination.parent == target.resolve():
                shutil.rmtree(destination)

    written: list[Path] = []
    for relative, data in contents.items():
        destination = target.joinpath(*relative.parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        written.append(destination)
    return written


def main() -> None:
    parser = argparse.ArgumentParser(description="Safely import packaged agent skills")
    parser.add_argument("archive", type=Path)
    parser.add_argument("--target", type=Path, default=Path("config/project/skills"))
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args()

    written = import_skills_zip(args.archive, args.target, replace=args.replace)
    for skill_file in sorted(path for path in written if path.name == "SKILL.md"):
        print(f"skill={skill_file.parent.name} sha256={sha256(skill_file)}")
    print(f"files_written={len(written)}")


if __name__ == "__main__":
    main()
