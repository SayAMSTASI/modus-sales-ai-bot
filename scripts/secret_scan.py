from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_PARTS = {".git", ".venv", ".pytest_cache", ".ruff_cache", "data", "__pycache__"}
TEXT_SUFFIXES = {
    ".example",
    ".html",
    ".json",
    ".md",
    ".ps1",
    ".py",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}
PATTERNS = {
    "OpenAI API key": re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b"),
    "Telegram bot token": re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{30,}\b"),
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
}


def candidate_files() -> list[Path]:
    files: list[Path] = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or any(part in SKIP_PARTS for part in path.parts):
            continue
        if path.name.startswith(".env") and path.name != ".env.example":
            continue
        if path.suffix.lower() in TEXT_SUFFIXES or path.name in {
            ".dockerignore",
            ".gitignore",
            "Dockerfile",
        }:
            files.append(path)
    return files


def main() -> None:
    files = candidate_files()
    findings: list[str] = []
    for path in files:
        text = path.read_text(encoding="utf-8", errors="ignore")
        for label, pattern in PATTERNS.items():
            if pattern.search(text):
                findings.append(f"{path.relative_to(ROOT)}: possible {label}")
    if findings:
        raise SystemExit("Secret scan failed:\n" + "\n".join(findings))
    print(f"secret-scan-ok files={len(files)}")


if __name__ == "__main__":
    main()
