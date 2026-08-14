from __future__ import annotations

import argparse
import hashlib
import shutil
from pathlib import Path


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description="Import a versioned sales-agent prompt bundle")
    parser.add_argument("--prompt", required=True, type=Path, help="UTF-8 prompt file")
    parser.add_argument("--skills-dir", type=Path, help="Folder containing <skill>/SKILL.md")
    parser.add_argument("--target", type=Path, default=Path("config/project"))
    args = parser.parse_args()

    if not args.prompt.is_file():
        raise SystemExit(f"Prompt not found: {args.prompt}")
    prompt_text = args.prompt.read_text(encoding="utf-8")
    if len(prompt_text.strip()) < 100:
        raise SystemExit("Prompt is unexpectedly short; import cancelled")
    skill_files = []
    if args.skills_dir:
        skill_files = sorted(args.skills_dir.glob("*/SKILL.md"))
        if not skill_files:
            raise SystemExit("No <skill>/SKILL.md files found; import cancelled")

    args.target.mkdir(parents=True, exist_ok=True)
    shutil.copy2(args.prompt, args.target / "prompt.md")
    target_skills = args.target / "skills"
    target_skills.mkdir(exist_ok=True)
    for source in skill_files:
        destination = target_skills / source.parent.name
        destination.mkdir(exist_ok=True)
        shutil.copy2(source, destination / "SKILL.md")

    print(f"prompt_sha256={sha256(args.target / 'prompt.md')}")
    for source in skill_files:
        target = target_skills / source.parent.name / "SKILL.md"
        print(f"skill={source.parent.name} sha256={sha256(target)}")


if __name__ == "__main__":
    main()

