"""Validate that a Windows client archive is complete and contains no secrets."""

import argparse
import re
import sys
import zipfile
from pathlib import PurePosixPath


REQUIRED_SUFFIXES = {
    "MedikTest-Collector/MedikTest-Collector.exe",
    "CLIENT_INSTRUCTIONS.md",
    "VERSION.txt",
}
FORBIDDEN_BASENAMES = {".env", "pilot.db"}
TEXT_SUFFIXES = {".txt", ".md", ".json", ".ini", ".cfg", ".yaml", ".yml"}
MAX_MEMBER_PATH = 205
SECRET_ASSIGNMENT = re.compile(
    r"SELFTEST_(?:USERNAME|PASSWORD)\s*=\s*(?!\s*(?:\r?\n|$))\S+",
    re.IGNORECASE,
)


def validate_archive(path: str) -> list[str]:
    errors: list[str] = []
    with zipfile.ZipFile(path) as archive:
        names = [PurePosixPath(info.filename.replace("\\", "/")) for info in archive.infolist()]
        normalized = {str(name).rstrip("/") for name in names}
        for suffix in REQUIRED_SUFFIXES:
            if not any(name.endswith(suffix) for name in normalized):
                errors.append("В архиве отсутствует обязательный файл: {}".format(suffix))
        for info, name in zip(archive.infolist(), names):
            normalized_name = str(name)
            if name.is_absolute() or ".." in name.parts:
                errors.append("В архиве найден небезопасный путь: {}".format(name))
            if len(normalized_name) > MAX_MEMBER_PATH:
                errors.append(
                    "Слишком длинный внутренний путь ({} символов): {}".format(
                        len(normalized_name), name
                    )
                )
            lowered_parts = [part.lower() for part in name.parts]
            basename = name.name.lower()
            if basename in FORBIDDEN_BASENAMES or basename.endswith(".db"):
                errors.append("В архив попал запрещённый файл: {}".format(name))
            if (
                any(part.startswith("mediktest") for part in lowered_parts)
                and any(part in {"data", "probes", "exports", "backups"} for part in lowered_parts)
            ):
                errors.append("В архив попал каталог рабочих данных: {}".format(name))
            if info.file_size > 1_000_000 or name.suffix.lower() not in TEXT_SUFFIXES:
                continue
            try:
                text = archive.read(info).decode("utf-8")
            except (UnicodeDecodeError, KeyError):
                continue
            if SECRET_ASSIGNMENT.search(text):
                errors.append("В текстовом файле найдены заполненные учётные данные: {}".format(name))
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("archive")
    args = parser.parse_args()
    errors = validate_archive(args.archive)
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    print("Client package validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
