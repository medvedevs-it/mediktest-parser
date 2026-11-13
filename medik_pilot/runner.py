import threading
import time
import uuid
import json
import math
import shutil
from typing import Any, Dict, Optional

from .collectors import DemoCollector, LiveSelftestCollector
from .collectors.live import CollectionConfigurationError
from .collectors.reh2 import Reh2Collector, Reh2TransientError, Reh2Error, Reh2SourceExhausted, Reh2RepeatedAttempt, CollectionInterrupted
from .config import DATA_DIR, DB_PATH, EXPORT_DIR, IMAGE_DIR, PROBE_DIR, Settings
from .domain import payload_status
from .comparison import test_identity
from .specialties import normalize_specialty
from .storage import Storage, utc_now


class RunManager:
    MAX_CONSECUTIVE_KIND_FAILURES = 12
    MAX_CONSECUTIVE_EMPTY_RESULTS = 3

    STOP_REASON_LABELS = {
        "max_attempts": "достигнут лимит попыток",
        "max_duration": "достигнут лимит времени",
        "max_requests": "достигнут лимит запросов",
        "reference_verified": "собрано заданное количество и выполнена дополнительная проверка",
        "user": "остановлено пользователем",
        "application_restart": "приостановлено после перезапуска приложения",
        "no_new_packages": "три последовательных пакета без новых уникальных тестов; полнота банка не подтверждена",
        "source_exhausted": "последовательность аккаунта REH2 закончилась; материалы сохранены, история не сброшена, полнота банка не подтверждена",
    }

    def __init__(self, storage: Storage):
        self.storage = storage
        self.storage.recover_stale_runs()
        self._lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._active_run_id: Optional[str] = None
        self._credentials: Optional[tuple[str, str]] = None

    def set_credentials(self, username: str, password: str) -> None:
        self._credentials = (username.strip(), password)

    def credentials_configured(self) -> bool:
        return bool(self._credentials and self._credentials[0] and self._credentials[1])

    def reset_all_materials(self) -> Dict[str, Any]:
        """Clear the complete bank only when no collection thread is active."""
        with self._lock:
            if self._thread and self._thread.is_alive():
                raise RuntimeError("Нельзя сбросить банк, пока выполняется сбор.")
            result: Dict[str, Any] = self.storage.reset_all_materials()
            removed_files = 0
            cleanup_warnings = []
            for directory in (IMAGE_DIR, EXPORT_DIR, PROBE_DIR):
                directory.mkdir(parents=True, exist_ok=True)
                for target in directory.iterdir():
                    try:
                        if target.is_dir():
                            shutil.rmtree(target)
                        else:
                            target.unlink()
                        removed_files += 1
                    except OSError as exc:
                        cleanup_warnings.append("{}: {}".format(target.name, exc))
            self._active_run_id = None
            self._thread = None
            self._stop_event = threading.Event()
            self._pause_event = threading.Event()
            result.update({
                "status": "reset",
                "removed_files": removed_files,
                "cleanup_warnings": cleanup_warnings,
            })
            return result

    def reset_specialty_materials(self, specialty: str) -> Dict[str, Any]:
        """Back up and clear one specialty without touching the other bank."""
        specialty = normalize_specialty(specialty)
        with self._lock:
            if self._thread and self._thread.is_alive():
                raise RuntimeError("Нельзя сбросить банк, пока выполняется сбор.")
            stamp = time.strftime("%Y%m%d-%H%M%S")
            backup_dir = DATA_DIR / "backups" / "{}-reset-{}".format(
                stamp, specialty.casefold().replace(" ", "-")
            )
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup_path = backup_dir / "pilot.db"
            if self.storage.path.is_file():
                self.storage.backup(backup_path)
            result = self.storage.reset_specialty_materials(specialty)
            run_ids = set(result.pop("deleted_run_ids", []))
            removed_files = 0
            warnings = []
            for directory in (EXPORT_DIR, PROBE_DIR):
                directory.mkdir(parents=True, exist_ok=True)
                for target in directory.iterdir():
                    if not run_ids or not any(run_id in target.name for run_id in run_ids):
                        continue
                    try:
                        if target.is_dir():
                            shutil.rmtree(target)
                        else:
                            target.unlink()
                        removed_files += 1
                    except OSError as exc:
                        warnings.append("{}: {}".format(target.name, exc))
            referenced_images = self.storage.referenced_image_storage_names()
            IMAGE_DIR.mkdir(parents=True, exist_ok=True)
            for target in IMAGE_DIR.iterdir():
                if not target.is_file() or target.name in referenced_images:
                    continue
                try:
                    target.unlink()
                    removed_files += 1
                except OSError as exc:
                    warnings.append("{}: {}".format(target.name, exc))
            result.update({
                "status": "reset",
                "backup_path": str(backup_path),
                "removed_files": removed_files,
                "cleanup_warnings": warnings,
            })
            return result

    def start(self, config: Dict[str, Any]) -> str:
        with self._lock:
            if self._thread and self._thread.is_alive():
                raise RuntimeError("Другой запуск уже выполняется.")
            run_id = str(uuid.uuid4())
            self.storage.create_run(run_id, config)
            self._stop_event = threading.Event()
            self._pause_event = threading.Event()
            self._active_run_id = run_id
            self._thread = threading.Thread(target=self._execute, args=(run_id, config), daemon=True)
            self._thread.start()
            return run_id

    def stop(self, run_id: str) -> None:
        with self._lock:
            run = self.storage.get_run(run_id)
            if not run:
                raise RuntimeError("Запуск не найден.")
            if (not self._thread or not self._thread.is_alive()) and run["status"] == "paused":
                self.storage.update_run(
                    run_id,
                    status="stopped",
                    stop_reason="user",
                    finished_at=utc_now(),
                )
                self.storage.add_event(run_id, "Приостановленный сбор остановлен пользователем.", "warning")
                return
            if run_id != self._active_run_id or not self._thread or not self._thread.is_alive():
                raise RuntimeError("Этот запуск уже не выполняется.")
            self._stop_event.set()
            self.storage.update_run(run_id, status="stopping")
            self.storage.add_event(run_id, "Запрошена мягкая остановка.", "warning")

    def pause(self, run_id: str) -> None:
        with self._lock:
            if run_id != self._active_run_id or not self._thread or not self._thread.is_alive():
                raise RuntimeError("Этот запуск сейчас не выполняется.")
            self._pause_event.set()
            self.storage.update_run(run_id, status="paused")
            self.storage.add_event(run_id, "Сбор поставлен на паузу.", "warning")

    def resume(self, run_id: str) -> None:
        with self._lock:
            run = self.storage.get_run(run_id)
            if not run:
                raise RuntimeError("Запуск не найден.")
            if (self.storage.reh2_state(run_id) or {}).get("phase") == "exhausted":
                raise RuntimeError(str(Reh2SourceExhausted()))
            if (self.storage.reh2_state(run_id) or {}).get("phase") == "repeated_attempt":
                raise RuntimeError(str(Reh2RepeatedAttempt()))
            if run["source_mode"] == "live" and not self.credentials_configured():
                raise RuntimeError("После перезапуска повторно укажите логин и пароль тренажёра.")
            if self._thread and self._thread.is_alive():
                if run_id != self._active_run_id or run["status"] != "paused":
                    raise RuntimeError("Другой запуск уже выполняется.")
                self._pause_event.clear()
                self.storage.update_run(run_id, status="running", stop_reason=None, error_message=None)
                self.storage.add_event(run_id, "Сбор продолжен.")
                return
            recoverable_reh2 = (run.get("test_source") == "reh2" and run["status"] == "failed"
                               and self.storage.reh2_state(run_id) is not None)
            if run["status"] != "paused" and not recoverable_reh2:
                raise RuntimeError("Продолжить можно только приостановленный запуск.")
            config = self.storage.run_config(run_id)
            if not config:
                raise RuntimeError("Не удалось восстановить параметры запуска.")
            self._stop_event = threading.Event()
            self._pause_event = threading.Event()
            self._active_run_id = run_id
            self._thread = threading.Thread(target=self._execute, args=(run_id, config), daemon=True)
            self._thread.start()

    def _collector(self, mode: str, config: Dict[str, Any], run_id: Optional[str] = None):
        if mode == "demo":
            return DemoCollector(specialty=config["specialty"])
        if mode == "live":
            settings = Settings.from_env()
            if self._credentials:
                settings = settings.with_credentials(*self._credentials)
            reference_tests = config.get("reference_tests")
            if reference_tests is None:
                reference_tests = config.get("max_tests", settings.sample_tests)
            legacy = LiveSelftestCollector(
                settings,
                specialty=config["specialty"],
                max_tests=max(1, min(int(reference_tests or settings.sample_tests), 200)),
                max_cases=1,
                allow_create_attempts=bool(config.get("allow_create_attempts", False)),
                allow_answer_submission=bool(config.get("allow_answer_submission", False)),
                progress_callback=lambda message: self.storage.add_event(self._active_run_id or "", message),
                stop_callback=self._stop_event.is_set,
                capture_callback=(
                    (lambda item, attempt: self.storage.stage_raw_item(run_id, item, attempt))
                    if run_id
                    else None
                ),
            )
            if config.get("test_source", "legacy") == "reh2" and config["material_type"] != "case":
                collector = Reh2Collector(settings, config["specialty"], self.storage, run_id, legacy,
                    bool(config.get("allow_create_attempts")), bool(config.get("allow_answer_submission")))
                collector.stop_callback = self._stop_event.is_set
                return collector
            return legacy
        raise ValueError("Unsupported source mode: {}".format(mode))

    def _collect_with_retry(self, collector, run_id: str, kind: str, attempt: int):
        last_error: Optional[Exception] = None
        # A mutating collector is retried only when it explicitly guarantees
        # that it can reopen the same active/completed external attempt.
        mutating = bool(getattr(collector, "allow_answer_submission", False))
        safe_resume = bool(getattr(collector, "supports_safe_resume", False))
        retry_limit = 3 if not mutating or safe_resume else 1
        for retry in range(1, retry_limit + 1):
            if self._stop_event.is_set():
                return []
            try:
                return collector.collect_attempt(kind, attempt)
            except CollectionConfigurationError:
                # This is not a transient browser/network failure. Retrying
                # twelve outer cycles only delays the actionable message and
                # can needlessly keep the account session open.
                raise
            except CollectionInterrupted:
                raise
            except Exception as exc:
                if kind == "test" and getattr(collector, "whole_test_packages", False) and not isinstance(exc, Reh2TransientError):
                    raise Reh2Error("REH2: внутренняя ошибка обработки; следующая попытка запрещена. {}".format(type(exc).__name__)) from exc
                last_error = exc
                self.storage.add_event(
                    run_id,
                    "Ошибка получения {} (попытка {}/{}): {}.".format(
                        "тестов" if kind == "test" else "кейсов", retry, retry_limit, exc
                    ),
                    "warning" if retry < retry_limit else "error",
                )
                if retry < retry_limit:
                    if hasattr(collector, "reset_session"):
                        try:
                            collector.reset_session()
                            self.storage.add_event(
                                run_id,
                                "Браузерная сессия сброшена перед безопасным повтором.",
                                "warning",
                            )
                        except Exception as reset_error:
                            self.storage.add_event(
                                run_id,
                                "Не удалось сбросить браузерную сессию: {}.".format(
                                    reset_error
                                ),
                                "warning",
                            )
                    self._stop_event.wait(min(2 ** (retry - 1), 4))
                elif hasattr(collector, "reset_session"):
                    # Leave the collector clean for a later outer-loop retry.
                    try:
                        collector.reset_session()
                    except Exception:
                        pass
        if kind == "test" and getattr(collector, "whole_test_packages", False):
            raise Reh2Error("REH2: сеть недоступна после трёх повторов. Контрольная точка сохранена; повторите продолжение позже.")
        raise RuntimeError(
            "Не удалось получить {} после {}: {}".format(
                "тесты" if kind == "test" else "ситуационные задачи",
                "одной безопасной попытки" if retry_limit == 1 else "трёх безопасных попыток",
                last_error,
            )
        )

    def _execute(self, run_id: str, config: Dict[str, Any]) -> None:
        existing = self.storage.get_run(run_id) or {}
        with self._lock:
            initial_status = "paused" if self._pause_event.is_set() else "running"
            if existing.get("started_at"):
                self.storage.update_run(
                    run_id,
                    status=initial_status,
                    stop_reason=None,
                    error_message=None,
                )
            else:
                self.storage.update_run(
                    run_id,
                    status=initial_status,
                    started_at=utc_now(),
                )
        self.storage.add_event(run_id, "Сбор запущен: {}.".format(config["source_mode"]))
        elapsed_before = float(existing.get("elapsed_seconds") or config.get("elapsed_seconds") or 0)
        started = time.monotonic()
        paused_seconds = 0.0

        def elapsed_seconds() -> float:
            return elapsed_before + max(0.0, time.monotonic() - started - paused_seconds)

        def wait_while_paused() -> None:
            nonlocal paused_seconds
            if not self._pause_event.is_set():
                return
            pause_started = time.monotonic()
            while self._pause_event.is_set() and not self._stop_event.is_set():
                time.sleep(0.2)
            paused_seconds += time.monotonic() - pause_started
        totals = {
            "seen": int(existing.get("items_seen") or 0),
            "new": int(existing.get("unique_items") or 0),
            "duplicate": int(existing.get("duplicate_items") or 0),
            "changed": int(existing.get("changed_items") or 0),
            "requests": int(existing.get("requests_made") or 0),
        }
        stop_reason = "max_attempts"
        collector = None
        try:
            collector = self._collector(config["source_mode"], config, run_id)
            if hasattr(collector, "pause_callback"):
                collector.pause_callback = wait_while_paused
            kinds = ["test", "case"] if config["material_type"] == "both" else [config["material_type"]]
            reference_tests = config.get("reference_tests")
            reference_cases = config.get("reference_cases")
            if reference_tests is None:
                reference_tests = config.get("max_tests", 0)
            if reference_cases is None:
                reference_cases = config.get("max_cases", 0)
            references = {
                "test": int(reference_tests or 0),
                "case": int(reference_cases or 0),
            }
            verification_percent = int(config.get("verification_percent", 0))
            verification_budget = {
                kind: (
                    0
                    if verification_percent == 0
                    else max(1, math.ceil(references[kind] * verification_percent / 100))
                )
                for kind in kinds
            }
            catalog_counts = {"test": 0, "case": 0}
            catalog_counts.update(self.storage.catalog_counts(config["source_mode"], config["specialty"]))
            run_seen_ids = self.storage.run_ready_identity_keys(run_id)
            run_counts = {
                "test": len(run_seen_ids.get("test", set())),
                "case": len(run_seen_ids.get("case", set())),
            }
            checkpoint = existing.get("checkpoint") or {}
            verification_seen = {"test": 0, "case": 0}
            verification_seen.update(checkpoint.get("verification_seen") or {})
            if getattr(collector, "whole_test_packages", False):
                verification_seen["test"] = self.storage.reh2_verification_seen(run_id, references["test"])
            last_processed = checkpoint.get("last_processed")
            no_new_packages = int(checkpoint.get("no_new_packages", 0))
            consecutive_failures = {"test": 0, "case": 0}
            consecutive_empty_results = {"test": 0, "case": 0}
            first_attempt = int(existing.get("attempts_completed") or 0) + 1
            for attempt in range(first_attempt, config["max_attempts"] + 1):
                if self._stop_event.is_set():
                    stop_reason = "user"
                    break
                wait_while_paused()
                if self._stop_event.is_set():
                    stop_reason = "user"
                    break
                elapsed_minutes = elapsed_seconds() / 60
                if elapsed_minutes >= config["max_duration_minutes"]:
                    stop_reason = "max_duration"
                    break
                attempt_seen = attempt_new = 0
                for kind in kinds:
                    if self._stop_event.is_set():
                        stop_reason = "user"
                        break
                    if totals["requests"] >= config.get("max_requests", 100000):
                        stop_reason = "max_requests"
                        break
                    pending_test_state = self.storage.reh2_state(run_id) if kind == "test" and getattr(collector, "whole_test_packages", False) else None
                    pending_package = pending_test_state and pending_test_state["phase"] != "committed"
                    if (
                        run_counts[kind] >= references[kind]
                        and verification_seen[kind] >= verification_budget[kind]
                        and not pending_package
                    ):
                        continue
                    self.storage.add_event(run_id, "Попытка {}: получение {}.".format(attempt, "тестов" if kind == "test" else "кейсов"))
                    try:
                        items = self._collect_with_retry(
                            collector, run_id, kind, attempt
                        )
                    except CollectionInterrupted:
                        stop_reason = "user"
                        break
                    except Reh2SourceExhausted as exc:
                        stop_reason = "source_exhausted"
                        self.storage.add_event(run_id, str(exc), "warning")
                        break
                    except Reh2RepeatedAttempt:
                        raise
                    except CollectionConfigurationError as exc:
                        self.storage.add_event(run_id, str(exc), "error")
                        raise RuntimeError(str(exc)) from exc
                    except Exception as exc:
                        consecutive_failures[kind] += 1
                        failure_count = consecutive_failures[kind]
                        if failure_count >= self.MAX_CONSECUTIVE_KIND_FAILURES:
                            raise RuntimeError(
                                "{} не удалось получить {} циклов подряд; "
                                "сбор остановлен для защиты аккаунта: {}".format(
                                    "Тесты" if kind == "test" else "Ситуационные задачи",
                                    failure_count,
                                    exc,
                                )
                            ) from exc
                        base_retry_delay = float(
                            config.get("error_retry_delay_seconds", 2)
                        )
                        retry_delay = min(
                            base_retry_delay * (2 ** min(failure_count - 1, 5)),
                            60,
                        )
                        self.storage.add_event(
                            run_id,
                            "{}: цикл пропущен после временной ошибки; "
                            "сбор продолжится через {} сек. "
                            "Последовательных сбоев: {}/{}.".format(
                                "Тесты" if kind == "test" else "Ситуационные задачи",
                                retry_delay,
                                failure_count,
                                self.MAX_CONSECUTIVE_KIND_FAILURES,
                            ),
                            "warning",
                        )
                        self._stop_event.wait(retry_delay)
                        continue
                    consecutive_failures[kind] = 0
                    if not items:
                        consecutive_empty_results[kind] += 1
                        empty_count = consecutive_empty_results[kind]
                        self.storage.add_event(
                            run_id,
                            "{}: сайт не вернул материалов ({}/{}).".format(
                                "Тесты" if kind == "test" else "Ситуационные задачи",
                                empty_count,
                                self.MAX_CONSECUTIVE_EMPTY_RESULTS,
                            ),
                            "warning",
                        )
                        if empty_count >= self.MAX_CONSECUTIVE_EMPTY_RESULTS:
                            raise RuntimeError(
                                "{} не получены с сайта после {} последовательных циклов. "
                                "Проверьте доступность попыток и разрешения на их создание и завершение.".format(
                                    "Тесты" if kind == "test" else "Ситуационные задачи",
                                    empty_count,
                                )
                            )
                    else:
                        consecutive_empty_results[kind] = 0
                    needed = max(0, references[kind] - run_counts[kind]) + max(
                        0, verification_budget[kind] - verification_seen[kind]
                    )
                    whole_package = kind == "test" and getattr(collector, "whole_test_packages", False)
                    if needed and not whole_package:
                        items = items[:needed]
                    counts = {"new": 0, "duplicate": 0, "changed": 0}
                    for item in items:
                        wait_while_paused()
                        if self._stop_event.is_set():
                            stop_reason = "user"
                            break
                        if totals["requests"] >= config.get("max_requests", 100000):
                            stop_reason = "max_requests"
                            break
                        in_verification = run_counts[kind] >= references[kind]
                        outcome, item_id = self.storage.store_item(
                            run_id, config["source_mode"], config["specialty"], item, attempt
                        )
                        if outcome == "replayed":
                            continue
                        stored = self.storage.stored_item(item_id) if whole_package else None
                        ready = stored["status"] == "ready" if stored else payload_status(item.kind, item.payload) == "ready"
                        counts[outcome] += 1
                        totals[outcome] += 1
                        totals["seen"] += 1
                        totals["requests"] += 1
                        attempt_seen += 1
                        if outcome in {"new", "changed"} and ready:
                            catalog_counts.update(
                                self.storage.catalog_counts(config["source_mode"], config["specialty"])
                            )
                        if ready:
                            key = test_identity({'payload':stored['payload'] if stored else item.payload}) if kind == 'test' else item.source_id
                            run_seen_ids.setdefault(kind, set()).add(key)
                            run_counts[kind] = len(run_seen_ids[kind])
                        if outcome == "new":
                            attempt_new += 1
                        if in_verification and ready:
                            verification_seen[kind] += 1
                        last_processed = {
                            "kind": item.kind,
                            "source_id": item.source_id,
                            "attempt": attempt,
                            "outcome": outcome,
                            "status": stored['status'] if stored else payload_status(item.kind, item.payload),
                        }
                        item_checkpoint = {
                            "attempt": attempt,
                            "catalog_counts": catalog_counts,
                            "run_counts": run_counts,
                            "verification_seen": verification_seen,
                            "last_processed": last_processed,
                            "no_new_packages": no_new_packages,
                        }
                        self.storage.update_run(
                            run_id,
                            items_seen=totals["seen"],
                            unique_items=totals["new"],
                            duplicate_items=totals["duplicate"],
                            changed_items=totals["changed"],
                            requests_made=totals["requests"],
                            elapsed_seconds=elapsed_seconds(),
                            checkpoint_json=json.dumps(item_checkpoint, ensure_ascii=False),
                        )
                    if whole_package and not self._stop_event.is_set() and stop_reason != "max_requests":
                        package_state = collector.acknowledge_attempt(kind)
                        no_new_packages = package_state["no_new_packages"]
                        excess = max(0, run_counts[kind] - references[kind])
                        if excess:
                            self.storage.add_event(run_id, "Пакет REH2 сохранён целиком: превышение цели на {} готовых уникальных тестов.".format(excess))
                    self.storage.add_attempt_stats(run_id, attempt, kind, counts)
                    if whole_package:
                        stats = self.storage.reh2_package_stats(run_id, attempt)
                        self.storage.add_event(run_id,
                            'Тесты REH2: получено {received}, готовых {ready}, '
                            'новых уникальных для запуска {new_unique}, уже есть в банке {existing}, '
                            'некорректных {invalid}.'.format(**stats))
                        for diagnostic in stats['diagnostics']:
                            self.storage.add_event(run_id, 'REH2: {} записей — {}'.format(
                                diagnostic['count'], diagnostic['reason']), 'warning')
                    else:
                        self.storage.add_event(
                            run_id,
                            "{}: новых {}, повторов {}, изменённых {}.".format(
                                "Тесты" if kind == "test" else "Кейсы",
                                counts["new"], counts["duplicate"], counts["changed"],
                            ),
                        )
                checkpoint = {
                    "attempt": attempt,
                    "catalog_counts": catalog_counts,
                    "run_counts": run_counts,
                    "verification_seen": verification_seen,
                    "last_processed": last_processed,
                    "no_new_packages": no_new_packages,
                }
                self.storage.update_run(
                    run_id,
                    attempts_completed=(attempt - 1 if self._stop_event.is_set() or stop_reason in ("max_requests", "source_exhausted") else attempt),
                    items_seen=totals["seen"],
                    unique_items=totals["new"],
                    duplicate_items=totals["duplicate"],
                    changed_items=totals["changed"],
                    requests_made=totals["requests"],
                    elapsed_seconds=elapsed_seconds(),
                    checkpoint_json=json.dumps(checkpoint, ensure_ascii=False),
                )
                if stop_reason in ("user", "max_requests", "source_exhausted"):
                    break
                if all(
                    run_counts[kind] >= references[kind]
                    and verification_seen[kind] >= verification_budget[kind]
                    for kind in kinds
                ):
                    stop_reason = "reference_verified"
                    self.storage.add_event(
                        run_id,
                        "Собрано заданное количество материалов и выполнен проверочный объём.",
                    )
                    break
                if totals["requests"] >= config.get("max_requests", 1000):
                    stop_reason = "max_requests"
                    self.storage.add_event(run_id, "Достигнут лимит запросов.")
                    break
                if attempt < config["max_attempts"] and config["delay_seconds"] > 0:
                    self._stop_event.wait(min(config["delay_seconds"], max(0, config["max_duration_minutes"] * 60 - elapsed_seconds())))
            status = (
                "stopped"
                if stop_reason == "user"
                else "completed"
                if stop_reason == "reference_verified"
                else "partial"
            )
            self.storage.add_event(
                run_id,
                "Сбор завершён: {}.".format(self.STOP_REASON_LABELS.get(stop_reason, stop_reason)),
            )
            self.storage.update_run(
                run_id,
                status=status,
                stop_reason=stop_reason,
                finished_at=utc_now(),
                items_seen=totals["seen"],
                unique_items=totals["new"],
                duplicate_items=totals["duplicate"],
                changed_items=totals["changed"],
                requests_made=totals["requests"],
                elapsed_seconds=elapsed_seconds(),
            )
        except Exception as exc:
            if config.get("test_source") == "reh2":
                totals = self.storage.reh2_run_totals(run_id)
            self.storage.add_event(run_id, str(exc), "error")
            self.storage.update_run(
                run_id,
                status="failed",
                stop_reason="repeated_attempt" if isinstance(exc, Reh2RepeatedAttempt) else "error",
                finished_at=utc_now(),
                error_message=str(exc),
                items_seen=totals["seen"],
                unique_items=totals["new"],
                duplicate_items=totals["duplicate"],
                changed_items=totals["changed"],
                requests_made=totals["requests"],
                elapsed_seconds=elapsed_seconds(),
            )
        finally:
            if collector and hasattr(collector, "close"):
                collector.close()
            with self._lock:
                self._active_run_id = None
