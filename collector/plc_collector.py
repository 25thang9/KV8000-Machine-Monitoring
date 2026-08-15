"""Production collector: KEYENCE PLC -> Django database.

- Chỉ đọc PLC bằng Host Link TCP (RDS), không ghi device.
- Hỗ trợ nhiều PLC, nhiều máy; cấu hình nằm trong database/admin.
- Một worker tối đa cho mỗi PLC tại một thời điểm.
- Poll nhanh nhưng chỉ ghi lịch sử theo chu kỳ hoặc khi dữ liệu thay đổi.
- Ghi heartbeat OFFLINE có kiểm soát khi mất kết nối.
"""

from __future__ import annotations

import hashlib
import logging
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass
from concurrent.futures import Future, ThreadPoolExecutor
from logging.handlers import RotatingFileHandler
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DASHBOARD_ROOT = PROJECT_ROOT / "dashboard"
if str(DASHBOARD_ROOT) not in sys.path:
    sys.path.insert(0, str(DASHBOARD_ROOT))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "machine_monitoring.settings")

import django  # noqa: E402

django.setup()

from django.conf import settings  # noqa: E402
from django.db import close_old_connections, transaction  # noqa: E402
from django.db.models import OuterRef, Prefetch, Subquery  # noqa: E402
from django.utils import timezone  # noqa: E402

from monitoring.models import (  # noqa: E402
    Machine,
    MachineCurrentState,
    MachineReading,
    PlcController,
    SignalMapping,
)

from collector.keyence_hostlink import (  # noqa: E402
    HostLinkConnectionError,
    HostLinkError,
    KeyenceHostLinkClient,
)


STOP_EVENT = threading.Event()
LOGGER = logging.getLogger("plc_collector")

# Một TCP session được giữ sống cho từng PLC giữa các chu kỳ poll.
# Main scheduler bảo đảm cùng một controller không bị poll đồng thời.
_CLIENT_LOCK = threading.Lock()
_CLIENTS: dict[int, tuple[tuple, KeyenceHostLinkClient]] = {}

# Một Machine chỉ được coi là online khi có đủ bộ tín hiệu chuẩn.
# Điều này ngăn cấu hình thiếu mapping bị đọc thành các giá trị mặc định 0
# và hiển thị sai trên dashboard.
REQUIRED_SIGNALS = frozenset(value for value, _label in SignalMapping.Signal.choices)


@dataclass(frozen=True)
class _BatchMapping:
    """Mapping proxy có key duy nhất trên toàn PLC để đọc block nhiều máy."""

    signal: str
    address: str
    data_type: str
    word_order: str
    scale: object
    offset: object
    machine_id: int
    original_signal: str


def _client_signature(controller: PlcController) -> tuple:
    return (
        controller.host,
        int(controller.port),
        int(controller.connect_timeout_ms),
        int(controller.read_timeout_ms),
    )


def _drop_persistent_client(controller_id: int) -> None:
    with _CLIENT_LOCK:
        entry = _CLIENTS.pop(controller_id, None)
    if entry is not None:
        try:
            entry[1].close()
        except Exception:
            pass


def _get_persistent_client(controller: PlcController) -> KeyenceHostLinkClient:
    signature = _client_signature(controller)
    with _CLIENT_LOCK:
        existing = _CLIENTS.get(controller.id)
        if existing is not None and existing[0] == signature:
            return existing[1]

        if existing is not None:
            try:
                existing[1].close()
            except Exception:
                pass

        client = KeyenceHostLinkClient(
            controller.host,
            controller.port,
            connect_timeout=controller.connect_timeout_ms / 1000,
            read_timeout=controller.read_timeout_ms / 1000,
        )
        client.connect()
        _CLIENTS[controller.id] = (signature, client)
        LOGGER.info(
            "%s TCP connected %s:%s (persistent)",
            controller.code,
            controller.host,
            controller.port,
        )
        return client


def _close_all_clients() -> None:
    with _CLIENT_LOCK:
        entries = list(_CLIENTS.values())
        _CLIENTS.clear()
    for _signature, client in entries:
        try:
            client.close()
        except Exception:
            pass


def _build_controller_batch(machines: list[Machine]):
    """Flatten mappings của mọi machine thành một batch trên cùng PLC.

    KeyenceHostLinkClient sẽ tự gom các device liên tiếp thành RDS block, vì
    vậy MR100..MR103 chỉ cần một lệnh và DM1000..DM1005 chỉ cần một lệnh.
    Các máy khác trên cùng PLC cũng được gom khi địa chỉ đủ gần nhau.
    """
    batch: list[_BatchMapping] = []
    mappings_by_machine: dict[int, list[SignalMapping]] = {}
    for machine in machines:
        mappings = list(machine.signal_mappings.all())
        mappings_by_machine[machine.id] = mappings
        for mapping in mappings:
            unique_key = f"M{machine.id}:{mapping.signal}"
            batch.append(
                _BatchMapping(
                    signal=unique_key,
                    address=mapping.address,
                    data_type=mapping.data_type,
                    word_order=mapping.word_order,
                    scale=mapping.scale,
                    offset=mapping.offset,
                    machine_id=machine.id,
                    original_signal=mapping.signal,
                )
            )
    return batch, mappings_by_machine


def _split_batch_values(
    batch: list[_BatchMapping],
    raw_all: dict[str, int | bool],
) -> dict[int, dict[str, int | bool]]:
    result: dict[int, dict[str, int | bool]] = {}
    for item in batch:
        if item.signal not in raw_all:
            continue
        result.setdefault(item.machine_id, {})[item.original_signal] = raw_all[item.signal]
    return result


class SingleInstanceError(RuntimeError):
    pass


class SingleInstanceLock:
    """OS-level file lock để tránh chạy 2 collector cùng lúc."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._file = None
        self._mutex_handle = None

    @staticmethod
    def _windows_kernel32():
        """Load kernel32 with 64-bit-safe HANDLE signatures."""
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [
            ctypes.c_void_p,
            wintypes.BOOL,
            wintypes.LPCWSTR,
        ]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        return kernel32

    def _acquire_windows_mutex(self) -> None:
        if os.name != "nt":
            return
        import ctypes

        kernel32 = self._windows_kernel32()
        root_key = hashlib.sha256(
            str(PROJECT_ROOT.resolve()).lower().encode("utf-8")
        ).hexdigest()[:16]
        mutex_name = f"Local\\KV8000MachineMonitoringCollector-{root_key}"

        # Clear stale thread-local last-error before CreateMutexW so the
        # ERROR_ALREADY_EXISTS check belongs to this exact call.
        ctypes.set_last_error(0)
        handle = kernel32.CreateMutexW(None, False, mutex_name)
        last_error = ctypes.get_last_error()
        if not handle:
            raise OSError(last_error, "Không tạo được Windows collector mutex.")

        # ERROR_ALREADY_EXISTS = 183. The existing kernel object is enough
        # to prove another process owns the collector instance for this project.
        if last_error == 183:
            kernel32.CloseHandle(handle)
            raise SingleInstanceError(
                "Đã có một plc_collector.py khác đang chạy."
            )
        self._mutex_handle = handle

    def _release_windows_mutex(self) -> None:
        if self._mutex_handle is None:
            return
        try:
            self._windows_kernel32().CloseHandle(self._mutex_handle)
        finally:
            self._mutex_handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._acquire_windows_mutex()

        # Trên Windows, msvcrt.locking() khóa theo byte và một process khác
        # có thể làm thao tác read() trên byte đang khóa ném PermissionError
        # trước cả khi code kịp thử acquire lock. Vì vậy chỉ kiểm tra kích
        # thước bằng fstat(), bảo đảm file có ít nhất 1 byte rồi khóa ngay.
        try:
            self._file = open(self.path, "a+", encoding="utf-8")
            if os.fstat(self._file.fileno()).st_size == 0:
                self._file.write("0")
                self._file.flush()

            self._file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if self._file is not None:
                self._file.close()
                self._file = None
            self._release_windows_mutex()
            raise SingleInstanceError(
                "Không lấy được collector lock; có thể Collector đã chạy "
                "hoặc file lock không có quyền truy cập."
            ) from exc

        # Chỉ đọc/ghi PID sau khi đã giữ lock thành công.
        self._file.seek(0)
        self._file.truncate()
        self._file.write(str(os.getpid()))
        self._file.flush()
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._file is None:
            return
        try:
            self._file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        finally:
            self._file.close()
            self._file = None
            self._release_windows_mutex()



def _configure_logging() -> None:
    if LOGGER.handlers:
        return

    level_name = os.getenv("COLLECTOR_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    LOGGER.setLevel(level)
    LOGGER.propagate = False

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(threadName)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    console.setLevel(level)
    LOGGER.addHandler(console)

    log_dir = PROJECT_ROOT / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        log_dir / "collector.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(level)
    LOGGER.addHandler(file_handler)


def _signal_handler(signum, _frame) -> None:
    LOGGER.info("Nhận signal %s, collector đang dừng an toàn...", signum)
    STOP_EVENT.set()


def _latest_readings_map(machine_ids: list[int]) -> dict[int, MachineReading]:
    if not machine_ids:
        return {}

    latest_id = (
        MachineReading.objects
        .filter(
            machine_id=OuterRef("pk"),
            source=MachineReading.DataSource.PLC,
        )
        .order_by("-recorded_at", "-pk")
        .values("pk")[:1]
    )
    pairs = list(
        Machine.objects
        .filter(pk__in=machine_ids)
        .annotate(latest_reading_id=Subquery(latest_id))
        .values_list("pk", "latest_reading_id")
    )
    reading_ids = [reading_id for _, reading_id in pairs if reading_id]
    if not reading_ids:
        return {}

    readings = {
        row.pk: row
        for row in MachineReading.objects.filter(pk__in=reading_ids)
    }
    return {
        machine_id: readings[reading_id]
        for machine_id, reading_id in pairs
        if reading_id in readings
    }



def _current_states_map(machine_ids: list[int]) -> dict[int, MachineCurrentState]:
    if not machine_ids:
        return {}
    return {
        state.machine_id: state
        for state in MachineCurrentState.objects.filter(machine_id__in=machine_ids)
    }


CURRENT_STATE_FIELDS = [
    "recorded_at",
    "plc_online",
    "run_bit",
    "stop_bit",
    "alarm_bit",
    "auto_mode_bit",
    "production_count",
    "cycle_time_ms",
    "alarm_code",
    "recipe_no",
    "source",
    "last_error",
    "updated_at",
]


def _prepare_current_state(
    machine: Machine,
    existing: MachineCurrentState | None,
    *,
    now,
    values: dict,
    error: str = "",
    update_recorded_at: bool = True,
) -> tuple[MachineCurrentState, bool]:
    is_new = existing is None
    state = existing or MachineCurrentState(machine=machine)

    # recorded_at mang nghĩa "thời điểm dữ liệu PLC hợp lệ gần nhất".
    # Khi PLC OFFLINE, collector vẫn cập nhật updated_at/last_error mỗi poll
    # nhưng không được đẩy recorded_at lên "bây giờ", nếu không giao diện sẽ
    # hiển thị sai rằng dữ liệu cũ chỉ mới 0 giây.
    if is_new or update_recorded_at:
        state.recorded_at = now
    for field in (
        "plc_online",
        "run_bit",
        "stop_bit",
        "alarm_bit",
        "auto_mode_bit",
        "production_count",
        "cycle_time_ms",
        "alarm_code",
        "recipe_no",
        "source",
    ):
        setattr(state, field, values[field])
    state.last_error = (error or "")[:4000]
    state.updated_at = now
    return state, is_new


def _write_current_states(
    creates: list[MachineCurrentState],
    updates: list[MachineCurrentState],
) -> None:
    if not creates and not updates:
        return
    with transaction.atomic():
        if creates:
            MachineCurrentState.objects.bulk_create(creates, batch_size=500)
        if updates:
            MachineCurrentState.objects.bulk_update(
                updates,
                fields=CURRENT_STATE_FIELDS,
                batch_size=500,
            )


def _age_seconds(reading, now) -> float:
    if reading is None:
        return float("inf")
    return max(0.0, (now - reading.recorded_at).total_seconds())


def _critical_signature_from_reading(reading: MachineReading | None):
    if reading is None:
        return None
    return (
        reading.plc_online,
        reading.run_bit,
        reading.stop_bit,
        reading.alarm_bit,
        reading.auto_mode_bit,
        reading.alarm_code,
        reading.recipe_no,
    )


def _critical_signature_from_values(values: dict):
    return (
        values["plc_online"],
        values["run_bit"],
        values["stop_bit"],
        values["alarm_bit"],
        values["auto_mode_bit"],
        values["alarm_code"],
        values["recipe_no"],
    )


def _should_persist_online(
    latest: MachineReading | None,
    values: dict,
    now,
    history_interval_seconds: int,
) -> bool:
    if latest is None:
        return True
    # Trạng thái/alarm/recipe được lưu ngay; count/cycle chỉ sample theo interval
    # để tránh hàng triệu row/ngày khi giá trị số thay đổi liên tục.
    if _critical_signature_from_reading(latest) != _critical_signature_from_values(values):
        return True
    return _age_seconds(latest, now) >= max(1, history_interval_seconds)


def _should_persist_offline(
    latest: MachineReading | None,
    now,
    offline_interval_seconds: int,
) -> bool:
    if latest is None or latest.plc_online:
        return True
    return _age_seconds(latest, now) >= max(1, offline_interval_seconds)


def _default_reading_values() -> dict:
    return {
        "plc_online": True,
        "run_bit": False,
        "stop_bit": False,
        "alarm_bit": False,
        "auto_mode_bit": False,
        "production_count": 0,
        "cycle_time_ms": None,
        "alarm_code": 0,
        "recipe_no": 0,
        "source": MachineReading.DataSource.PLC,
    }


def _offline_values(latest) -> dict:
    """OFFLINE giữ last-known numeric values nhưng reset các bit trạng thái."""
    values = _default_reading_values()
    values["plc_online"] = False
    if latest is not None:
        for field in (
            "production_count",
            "cycle_time_ms",
            "alarm_code",
            "recipe_no",
        ):
            values[field] = getattr(latest, field)
    return values


def _apply_signal_values(mappings: list[SignalMapping], raw: dict[str, int | bool]) -> dict:
    values = _default_reading_values()
    for mapping in mappings:
        if mapping.signal not in raw:
            continue
        field = mapping.field_name
        value = raw[mapping.signal]
        if mapping.data_type == SignalMapping.DataType.BIT:
            values[field] = bool(value)
        else:
            numeric = int(value)
            if field == "production_count":
                if not -(2**63) <= numeric <= (2**63 - 1):
                    raise ValueError(f"{mapping.address}: production_count vượt BIGINT")
            elif not -(2**31) <= numeric <= (2**31 - 1):
                raise ValueError(f"{mapping.address}: {field} vượt INTEGER 32-bit")
            values[field] = numeric
    return values


def _mark_controller(
    controller_id: int,
    *,
    seen: bool,
    error: str = "",
) -> None:
    now = timezone.now()
    controller = PlcController.objects.filter(pk=controller_id).first()
    if controller is None:
        return

    controller.last_poll_at = now
    if seen:
        controller.last_seen_at = now
        controller.consecutive_failures = 0
    else:
        controller.consecutive_failures = int(controller.consecutive_failures) + 1
    controller.last_error = error[:4000]
    controller.save(
        update_fields=[
            "last_poll_at",
            "last_seen_at",
            "consecutive_failures",
            "last_error",
            "updated_at",
        ]
    )


def _write_rows(rows: list[MachineReading]) -> None:
    if not rows:
        return
    with transaction.atomic():
        MachineReading.objects.bulk_create(rows, batch_size=500)


def _controller_machines(controller_id: int):
    mapping_qs = SignalMapping.objects.filter(is_enabled=True).order_by("signal")
    return list(
        Machine.objects
        .filter(controller_id=controller_id, is_active=True)
        .prefetch_related(Prefetch("signal_mappings", queryset=mapping_qs))
        .order_by("code")
    )


def poll_controller(controller_id: int) -> dict:
    close_old_connections()
    try:
        controller = PlcController.objects.get(pk=controller_id, is_active=True)
        machines = _controller_machines(controller_id)
        machine_ids = [machine.id for machine in machines]
        latest_map = _latest_readings_map(machine_ids)
        current_map = _current_states_map(machine_ids)
        now = timezone.now()

        if not machines:
            _mark_controller(
                controller_id,
                seen=False,
                error="PLC active nhưng chưa có Machine active được gán.",
            )
            return {"controller": controller.code, "online": False, "written": 0}

        history_rows: list[MachineReading] = []
        current_creates: list[MachineCurrentState] = []
        current_updates: list[MachineCurrentState] = []
        machine_errors: list[str] = []
        successful_machine_reads = 0

        def queue_current(
            machine: Machine,
            values: dict,
            error: str = "",
            *,
            update_recorded_at: bool = True,
        ) -> None:
            state, is_new = _prepare_current_state(
                machine,
                current_map.get(machine.id),
                now=now,
                values=values,
                error=error,
                update_recorded_at=update_recorded_at,
            )
            current_map[machine.id] = state
            if is_new:
                current_creates.append(state)
            else:
                current_updates.append(state)

        def queue_offline(machine: Machine, message: str) -> None:
            latest_history = latest_map.get(machine.id)
            prior_current = current_map.get(machine.id)
            offline_values = _offline_values(prior_current or latest_history)
            queue_current(
                machine,
                offline_values,
                message,
                update_recorded_at=False,
            )
            if _should_persist_offline(
                latest_history,
                now,
                controller.offline_write_interval_seconds,
            ):
                history_rows.append(
                    MachineReading(
                        machine=machine,
                        recorded_at=now,
                        **offline_values,
                    )
                )

        try:
            # Giữ socket TCP sống giữa các poll; chỉ reconnect khi socket lỗi
            # hoặc cấu hình endpoint/timeout thay đổi.
            client = _get_persistent_client(controller)

            # Chỉ đưa Machine có đủ bộ mapping chuẩn vào batch. Cấu hình thiếu
            # một signal không được phép "online giả" với giá trị mặc định 0.
            configured_machines: list[Machine] = []
            for machine in machines:
                mappings = list(machine.signal_mappings.all())
                mapped_signals = {mapping.signal for mapping in mappings}
                missing = REQUIRED_SIGNALS - mapped_signals
                if not mappings:
                    message = f"{machine.code}: chưa cấu hình SignalMapping"
                    machine_errors.append(message)
                    queue_offline(machine, message)
                    continue
                if missing:
                    message = (
                        f"{machine.code}: thiếu SignalMapping "
                        + ", ".join(sorted(missing))
                    )
                    machine_errors.append(message)
                    queue_offline(machine, message)
                    continue
                configured_machines.append(machine)

            batch, mappings_by_machine = _build_controller_batch(configured_machines)

            if configured_machines:
                try:
                    # Một read_mappings cho toàn PLC. Client tự chia thành các
                    # contiguous span theo prefix/địa chỉ, giảm round-trip TCP.
                    raw_all = client.read_mappings(batch)
                    raw_by_machine = _split_batch_values(batch, raw_all)
                    now = timezone.now()

                    for machine in configured_machines:
                        latest_history = latest_map.get(machine.id)
                        mappings = mappings_by_machine[machine.id]
                        raw = raw_by_machine.get(machine.id, {})
                        values = _apply_signal_values(mappings, raw)
                        queue_current(machine, values)
                        successful_machine_reads += 1
                        if _should_persist_online(
                            latest_history,
                            values,
                            now,
                            controller.history_interval_seconds,
                        ):
                            history_rows.append(
                                MachineReading(
                                    machine=machine,
                                    recorded_at=now,
                                    **values,
                                )
                            )

                except (OSError, HostLinkConnectionError) as exc:
                    # Socket hỏng: drop session để poll kế tiếp reconnect.
                    _drop_persistent_client(controller_id)
                    error_text = str(exc)
                    machine_errors.append(error_text)
                    now = timezone.now()
                    for machine in configured_machines:
                        queue_offline(machine, error_text)

                except (HostLinkError, ValueError) as exc:
                    # Protocol/mapping batch lỗi: không ghi dữ liệu sai vào DB.
                    error_text = str(exc)
                    machine_errors.append(error_text)
                    now = timezone.now()
                    for machine in configured_machines:
                        queue_offline(machine, error_text)

            _write_current_states(current_creates, current_updates)
            _write_rows(history_rows)
            error_text = " | ".join(machine_errors[:10])
            controller_seen = successful_machine_reads > 0
            _mark_controller(controller_id, seen=controller_seen, error=error_text)
            return {
                "controller": controller.code,
                "online": controller_seen,
                "machines": len(machines),
                "successful": successful_machine_reads,
                "errors": len(machine_errors),
                "written": len(history_rows),
                "error": error_text,
            }

        except (OSError, HostLinkError) as exc:
            # Không mở được socket/PLC đóng kết nối: toàn bộ máy của PLC là OFFLINE.
            _drop_persistent_client(controller_id)
            error_text = str(exc)
            for machine in machines:
                queue_offline(machine, error_text)
            _write_current_states(current_creates, current_updates)
            _write_rows(history_rows)
            _mark_controller(controller_id, seen=False, error=error_text)
            return {
                "controller": controller.code,
                "online": False,
                "machines": len(machines),
                "written": len(history_rows),
                "error": error_text,
            }
    finally:
        close_old_connections()


def _load_controller_schedule() -> dict[int, float]:
    close_old_connections()
    try:
        return {
            row["id"]: max(0.1, int(row["poll_interval_ms"]) / 1000)
            for row in PlcController.objects.filter(is_active=True).values(
                "id", "poll_interval_ms"
            )
        }
    finally:
        close_old_connections()


def main() -> int:
    _configure_logging()
    signal.signal(signal.SIGINT, _signal_handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _signal_handler)

    max_workers = max(1, int(getattr(settings, "COLLECTOR_MAX_WORKERS", 16)))
    refresh_seconds = max(
        1.0,
        float(getattr(settings, "COLLECTOR_CONFIG_REFRESH_SECONDS", 5)),
    )

    LOGGER.info("============================================================")
    LOGGER.info("KV8000 PRODUCTION PLC COLLECTOR")
    LOGGER.info("Mode: READ-ONLY Host Link TCP / RDS")
    LOGGER.info("Workers tối đa: %s", max_workers)
    LOGGER.info("============================================================")

    next_due: dict[int, float] = {}
    in_flight: dict[int, Future] = {}
    schedule: dict[int, float] = {}
    next_refresh = 0.0

    with ThreadPoolExecutor(
        max_workers=max_workers,
        thread_name_prefix="PLC",
    ) as executor:
        while not STOP_EVENT.is_set():
            now_mono = time.monotonic()

            if now_mono >= next_refresh:
                try:
                    schedule = _load_controller_schedule()
                    active_ids = set(schedule)
                    for controller_id in list(next_due):
                        if controller_id not in active_ids and controller_id not in in_flight:
                            next_due.pop(controller_id, None)
                            # Controller vừa bị disable/xóa khỏi schedule:
                            # đóng persistent TCP session để không giữ socket thừa.
                            _drop_persistent_client(controller_id)
                    for controller_id in active_ids:
                        next_due.setdefault(controller_id, now_mono)
                    if not schedule:
                        LOGGER.warning(
                            "Chưa có PlcController active. Cấu hình trong /admin/ "
                            "hoặc chạy lệnh bootstrap_plc."
                        )
                except Exception:
                    LOGGER.exception("Không đọc được cấu hình PLC từ database")
                next_refresh = now_mono + refresh_seconds

            for controller_id, future in list(in_flight.items()):
                if not future.done():
                    continue
                in_flight.pop(controller_id, None)
                interval = schedule.get(controller_id, 1.0)
                next_due[controller_id] = time.monotonic() + interval
                try:
                    result = future.result()
                    if result.get("online"):
                        if result.get("errors", 0) and result.get("written", 0):
                            LOGGER.warning(
                                "%s ONLINE nhưng có %s machine lỗi | written=%s",
                                result.get("controller"),
                                result.get("errors", 0),
                                result.get("written", 0),
                            )
                        else:
                            # Poll ONLINE bình thường không spam log ở mức INFO.
                            LOGGER.debug(
                                "%s ONLINE | machines=%s | written=%s | errors=%s",
                                result.get("controller"),
                                result.get("machines", 0),
                                result.get("written", 0),
                                result.get("errors", 0),
                            )
                    else:
                        log_fn = LOGGER.warning if result.get("written", 0) else LOGGER.debug
                        log_fn(
                            "%s OFFLINE | written=%s | %s",
                            result.get("controller"),
                            result.get("written", 0),
                            result.get("error", ""),
                        )
                except Exception:
                    LOGGER.exception("Worker PLC %s bị lỗi ngoài dự kiến", controller_id)

            now_mono = time.monotonic()
            for controller_id, interval in schedule.items():
                if controller_id in in_flight:
                    continue
                if now_mono < next_due.get(controller_id, now_mono):
                    continue
                in_flight[controller_id] = executor.submit(
                    poll_controller,
                    controller_id,
                )
                # Chặn submit lặp trước khi future được đưa vào xử lý.
                next_due[controller_id] = now_mono + interval

            STOP_EVENT.wait(0.02)

    _close_all_clients()
    LOGGER.info("Collector đã dừng.")
    return 0


if __name__ == "__main__":
    try:
        with SingleInstanceLock(PROJECT_ROOT / "logs" / "collector.lock"):
            raise SystemExit(main())
    except SingleInstanceError as exc:
        print(f"COLLECTOR NOT STARTED: {exc}", file=sys.stderr)
        raise SystemExit(2)
