import os
import sys
from dataclasses import dataclass
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent


def runtime_data_dir() -> Path:
    configured = os.getenv("MEDIKTEST_DATA_DIR")
    if configured:
        return Path(configured).expanduser()
    if getattr(sys, "frozen", False) and os.name == "nt":
        return Path(os.getenv("LOCALAPPDATA", str(Path.home()))) / "MedikTest"
    return ROOT_DIR / "data"


DATA_DIR = runtime_data_dir()
EXPORT_DIR = DATA_DIR / "exports"
PROBE_DIR = DATA_DIR / "probes"
IMAGE_DIR = DATA_DIR / "images"
DB_PATH = DATA_DIR / "pilot.db"


def load_dotenv(path: Path = ROOT_DIR / ".env") -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class Settings:
    base_url: str
    username: str
    password: str
    headless: bool
    sample_tests: int
    sample_cases: int
    case_questions: int

    def with_credentials(self, username: str, password: str) -> "Settings":
        return Settings(
            base_url=self.base_url,
            username=username,
            password=password,
            headless=self.headless,
            sample_tests=self.sample_tests,
            sample_cases=self.sample_cases,
            case_questions=self.case_questions,
        )

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        return cls(
            base_url=os.getenv("SELFTEST_BASE_URL", "https://selftest.mededtech.ru/").rstrip("/") + "/",
            username=os.getenv("SELFTEST_USERNAME", ""),
            password=os.getenv("SELFTEST_PASSWORD", ""),
            headless=os.getenv("SELFTEST_HEADLESS", "true").lower() not in {"0", "false", "no"},
            sample_tests=max(1, min(10, int(os.getenv("SELFTEST_SAMPLE_TESTS", "3")))),
            sample_cases=max(1, min(10, int(os.getenv("SELFTEST_SAMPLE_CASES", "1")))),
            case_questions=max(1, min(12, int(os.getenv("SELFTEST_CASE_QUESTIONS", "12")))),
        )


def ensure_directories() -> None:
    for directory in (DATA_DIR, EXPORT_DIR, PROBE_DIR, IMAGE_DIR):
        directory.mkdir(parents=True, exist_ok=True)


def public_base_url() -> str:
    """Return the externally reachable collector URL used in Markdown exports."""
    load_dotenv()
    return os.getenv("MEDIKTEST_PUBLIC_BASE_URL", "http://127.0.0.1:8765").strip().rstrip("/")
