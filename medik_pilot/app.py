from datetime import datetime
import mimetypes
from pathlib import Path
import re
from typing import Literal, Optional
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Query, Request
from starlette.concurrency import run_in_threadpool
from starlette.background import BackgroundTask
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field, model_validator

from .exporter import (
    export_audit_xlsx,
    export_cases_xlsx,
    export_images_zip,
    export_json,
    export_tests_xlsx,
    export_xlsx,
    export_current_bank_xlsx,
)
from .config import DATA_DIR, IMAGE_DIR
from .comparison import ComparisonManager, ComparisonError, MAX_BYTES
from .images import IMAGE_EXTENSIONS, detect_image_type
from .runner import RunManager
from .specialties import DEFAULT_SPECIALTY, SUPPORTED_SPECIALTIES, normalize_specialty
from .storage import Storage


storage = Storage()
manager = RunManager(storage)
comparisons = ComparisonManager(storage, DATA_DIR / "comparisons")
app = FastAPI(title="Easy Station Collector", version="1.6.1")
INDEX_PATH = Path(__file__).resolve().parent / "web" / "index.html"
BRAND_LOGO_PATH = INDEX_PATH.parent / "easy-station-logo.png"


class RunRequest(BaseModel):
    source_mode: Literal["demo", "live"] = "live"
    # Applies only to newly submitted runs; persisted legacy runs keep their source.
    test_source: Literal["legacy", "reh2"] = "reh2"
    material_type: Literal["test", "case", "both"] = "both"
    specialty: str = DEFAULT_SPECIALTY
    document_mode: Literal["catalog", "new"] = "catalog"
    document_name: str = Field(default="МедикТест Лечебное дело", min_length=1, max_length=120)
    reference_tests: int = Field(default=0, ge=0, le=100000)
    reference_cases: int = Field(default=0, ge=0, le=100000)
    verification_percent: int = Field(default=15, ge=0, le=100)
    max_attempts: int = Field(default=1000, ge=1, le=100000)
    max_requests: int = Field(default=100000, ge=1, le=10000000)
    max_duration_minutes: int = Field(default=240, ge=1, le=10080)
    allow_create_attempts: bool = False
    allow_answer_submission: bool = False
    delay_seconds: float = Field(default=0.5, ge=0, le=30)
    # Compatibility with the pilot API. New clients use reference_*.
    max_tests: Optional[int] = Field(default=None, ge=0, le=100000, exclude=True)
    max_cases: Optional[int] = Field(default=None, ge=0, le=100000, exclude=True)

    @model_validator(mode="after")
    def normalize_legacy_limits(self):
        self.specialty = normalize_specialty(self.specialty)
        if self.reference_tests == 0 and self.max_tests is not None:
            self.reference_tests = self.max_tests
        if self.reference_cases == 0 and self.max_cases is not None:
            self.reference_cases = self.max_cases
        if self.material_type == "test":
            self.reference_cases = 0
        elif self.material_type == "case":
            self.reference_tests = 0
        if self.document_mode == "new":
            self.verification_percent = 0
            # A new document must be collected from the source. Creating and
            # completing attempts is required to reveal full answer keys and
            # the post-question sections of situational tasks.
            self.allow_create_attempts = True
            self.allow_answer_submission = True
        self.document_name = self.document_name.strip()
        return self


class CredentialsRequest(BaseModel):
    username: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=1, max_length=200)


class CatalogResetRequest(BaseModel):
    confirmation: Literal["RESET_ALL_MATERIALS", "RESET_SPECIALTY_MATERIALS"]
    specialty: Optional[str] = None


class ComparisonStartRequest(BaseModel):
    sheet: str = Field(min_length=1, max_length=31)
    specialty: str = DEFAULT_SPECIALTY


@app.get("/assets/comparison.js", include_in_schema=False)
def comparison_script():
    return FileResponse(INDEX_PATH.parent / "comparison.js", media_type="text/javascript",
                        headers={"Cache-Control": "no-cache"})


@app.post("/api/comparisons/uploads", status_code=202)
async def comparison_upload(request: Request, filename: str = Query(max_length=255)):
    content = bytearray()
    async for chunk in request.stream():
        if len(content) + len(chunk) > MAX_BYTES:
            raise HTTPException(413, "Размер файла превышает 20 МБ.")
        content.extend(chunk)
    try:
        return await run_in_threadpool(comparisons.upload, bytes(content), filename)
    except ComparisonError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/comparisons/{identity}")
def comparison_status(identity: str):
    try:
        return comparisons.get(identity)
    except ComparisonError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post("/api/comparisons/{identity}/start", status_code=202)
def comparison_start(identity: str, body: ComparisonStartRequest):
    try:
        return comparisons.start(identity, body.sheet, body.specialty)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/comparisons/{identity}/report.xlsx")
def comparison_report(identity: str):
    meta = comparison_status(identity)
    if meta["status"] != "completed":
        raise HTTPException(409, "Отчёт ещё не готов.")
    return FileResponse(comparisons.directory(identity) / "report.xlsx",
                        filename="Сравнение_{}_{}.xlsx".format(meta["specialty"], identity[:8]),
                        headers={"Cache-Control": "no-store"})


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse(INDEX_PATH.read_text(encoding="utf-8"))


@app.get("/assets/mediktest-logo.png", include_in_schema=False)
@app.get("/assets/easy-station-logo.png")
def brand_logo():
    return FileResponse(
        BRAND_LOGO_PATH,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/api/health")
def health():
    return {"status": "ok", "credentials_configured": manager.credentials_configured()}


@app.get("/api/assets/images/{digest}/{filename}")
def public_image(digest: str, filename: str):
    """Serve a content-addressed case image for Markdown rendered by the client."""
    digest = digest.strip().lower()
    suffix = Path(filename).suffix.lower()
    if not re.fullmatch(r"[0-9a-f]{64}", digest) or suffix not in IMAGE_EXTENSIONS:
        raise HTTPException(status_code=404, detail="Изображение не найдено.")
    target = IMAGE_DIR / "{}{}".format(digest, suffix)
    if not target.is_file():
        for candidate_suffix in IMAGE_EXTENSIONS:
            candidate = IMAGE_DIR / "{}{}".format(digest, candidate_suffix)
            if not candidate.is_file():
                continue
            detected_suffix, _detected_type = detect_image_type(candidate.read_bytes())
            if detected_suffix == suffix:
                target = candidate
                break
        else:
            raise HTTPException(status_code=404, detail="Изображение не найдено.")
    _detected_suffix, detected_type = detect_image_type(target.read_bytes())
    media_type = detected_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
    return FileResponse(
        target,
        media_type=media_type,
        headers={
            "Cache-Control": "public, max-age=31536000, immutable",
            "Content-Disposition": "inline; filename*=UTF-8''{}".format(
                quote(Path(filename).name, safe="")
            ),
        },
    )


@app.get("/api/config/status")
def config_status():
    return {
        "credentials_configured": manager.credentials_configured(),
        "specialty": DEFAULT_SPECIALTY,
        "specialties": list(SUPPORTED_SPECIALTIES),
    }


@app.post("/api/config/credentials")
def configure_credentials(request: CredentialsRequest):
    manager.set_credentials(request.username, request.password)
    return {
        "status": "configured",
        "specialty": DEFAULT_SPECIALTY,
        "specialties": list(SUPPORTED_SPECIALTIES),
    }


@app.post("/api/catalog/reset")
def reset_catalog(request: CatalogResetRequest):
    try:
        if request.confirmation == "RESET_ALL_MATERIALS":
            return manager.reset_all_materials()
        if not request.specialty:
            raise HTTPException(
                status_code=422,
                detail="Для scoped-сброса укажите специальность.",
            )
        return manager.reset_specialty_materials(request.specialty)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/catalog/counts")
def catalog_counts(specialty: str = Query(DEFAULT_SPECIALTY, min_length=1)):
    """Return live-bank counts for the selected specialty.

    The bank card is switchable independently of the currently displayed run,
    so it must read the selected specialty directly instead of reusing the
    last run's snapshot (which caused mismatched headings and counts).
    """
    try:
        canonical = normalize_specialty(specialty)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    counts = storage.export_bank_counts(canonical)
    return {
        "specialty": canonical,
        "test": int(counts.get("test", 0)),
        "case": int(counts.get("case", 0)),
        "total": int(counts.get("test", 0)) + int(counts.get("case", 0)),
    }


@app.post("/api/runs", status_code=201)
def create_run(request: RunRequest):
    if request.material_type in {"test", "both"} and request.reference_tests < 1:
        raise HTTPException(status_code=422, detail="Укажите количество тестов для текущего запуска.")
    if request.material_type in {"case", "both"} and request.reference_cases < 1:
        raise HTTPException(status_code=422, detail="Укажите количество ситуационных задач для текущего запуска.")
    if request.source_mode == "live" and not manager.credentials_configured():
        raise HTTPException(status_code=401, detail="Сначала укажите логин и пароль тренажёра.")
    try:
        run_id = manager.start(request.model_dump())
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "id": run_id,
        "reference_tests": request.reference_tests,
        "reference_cases": request.reference_cases,
    }


@app.get("/api/runs/latest")
def latest_run():
    return storage.latest_run() or {}


@app.get("/api/runs/{run_id}")
def get_run(run_id: str):
    run = storage.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Запуск не найден.")
    return run


@app.get("/api/runs/{run_id}/catalog-audit")
def catalog_audit(
    run_id: str,
    kind: Literal["all", "test", "case"] = "all",
    status: Literal["all", "confirmed", "not_checked"] = "not_checked",
    limit: int = 200,
    offset: int = 0,
):
    try:
        return storage.catalog_audit_items(
            run_id,
            kind=kind,
            audit_status=status,
            limit=max(1, min(limit, 500)),
            offset=max(0, offset),
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Запуск не найден.") from exc


@app.post("/api/runs/{run_id}/stop")
def stop_run(run_id: str):
    try:
        manager.stop(run_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"status": "stopping"}


@app.post("/api/runs/{run_id}/pause")
def pause_run(run_id: str):
    try:
        manager.pause(run_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"status": "pausing"}


@app.post("/api/runs/{run_id}/resume")
def resume_run(run_id: str):
    try:
        manager.resume(run_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"status": "running"}


@app.get("/api/runs")
def list_runs(limit: int = 20):
    return storage.list_runs(max(1, min(limit, 100)))


def export_filename(run_id: str, suffix: str, material_label: str = "") -> str:
    run = storage.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Запуск не найден.")
    raw_date = run.get("finished_at") or run.get("created_at")
    try:
        timestamp = datetime.fromisoformat(raw_date).astimezone().strftime("%Y-%m-%d_%H-%M")
    except (TypeError, ValueError):
        timestamp = datetime.now().astimezone().strftime("%Y-%m-%d_%H-%M")
    document_name = str(run.get("document_name") or "МедикТест Лечебное дело").strip()
    safe_name = re.sub(r'[<>:"/\\|?*]+', "_", document_name).strip(" .")
    label = "_{}".format(material_label) if material_label else ""
    return "{}{}_{}.{}".format(safe_name or "МедикТест", label, timestamp, suffix)


@app.get("/api/runs/{run_id}/export.json")
def download_json(run_id: str):
    filename = export_filename(run_id, "json")
    target = export_json(storage, run_id)
    return FileResponse(target, filename=filename, media_type="application/json")


@app.get("/api/runs/{run_id}/export.xlsx")
def download_xlsx(run_id: str):
    filename = export_filename(run_id, "xlsx")
    target = export_xlsx(storage, run_id)
    return FileResponse(
        target,
        filename=filename,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.get("/api/catalog/export.xlsx")
def download_current_bank(specialty: str):
    try:
        specialty = normalize_specialty(specialty)
        target = export_current_bank_xlsx(storage, specialty)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return FileResponse(target, filename="Банк_{}_{}.xlsx".format(specialty, datetime.now().strftime('%Y-%m-%d')),
                        background=BackgroundTask(target.unlink, missing_ok=True),
                        headers={'Cache-Control':'no-store'})


@app.get("/api/runs/{run_id}/export.images.zip")
def download_images_zip(run_id: str):
    filename = export_filename(run_id, "zip", "Изображения")
    try:
        target = export_images_zip(storage, run_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return FileResponse(target, filename=filename, media_type="application/zip")


@app.get("/api/runs/{run_id}/export.actuality.xlsx")
def download_actuality_xlsx(run_id: str):
    filename = export_filename(run_id, "xlsx", "Актуальность")
    try:
        target = export_audit_xlsx(storage, run_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Запуск не найден.") from exc
    return FileResponse(
        target,
        filename=filename,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.get("/api/runs/{run_id}/export.tests.xlsx")
def download_tests_xlsx(run_id: str):
    filename = export_filename(run_id, "xlsx", "Тесты")
    target = export_tests_xlsx(storage, run_id)
    return FileResponse(
        target,
        filename=filename,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.get("/api/runs/{run_id}/export.cases.xlsx")
def download_cases_xlsx(run_id: str):
    filename = export_filename(run_id, "xlsx", "Ситуационные_задачи")
    target = export_cases_xlsx(storage, run_id)
    return FileResponse(
        target,
        filename=filename,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
