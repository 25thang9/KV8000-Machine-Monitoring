"""
MOCK COLLECTOR - 50 MACHINES STRESS TEST

- Tạo/duy trì MACHINE-01 ... MACHINE-50.
- Không kết nối PLC thật.
- Ghi dữ liệu MOCK vào PostgreSQL theo lô.
- Có RUN / STOP / ALARM / OFFLINE(stale) / UNKNOWN.
- Poll mặc định 10 giây để stress test ổn định.
"""

from __future__ import annotations

import os
import random
import sys
import time
from datetime import timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DASHBOARD_ROOT = PROJECT_ROOT / "dashboard"

if str(DASHBOARD_ROOT) not in sys.path:
    sys.path.insert(0, str(DASHBOARD_ROOT))

os.environ.setdefault(
    "DJANGO_SETTINGS_MODULE",
    "machine_monitoring.settings",
)

import django  # noqa: E402

django.setup()

from django.db import (  # noqa: E402
    InterfaceError,
    OperationalError,
    close_old_connections,
    transaction,
)
from django.utils import timezone  # noqa: E402

from monitoring.models import Machine, MachineReading  # noqa: E402


# ============================================================
# CẤU HÌNH
# ============================================================

TOTAL_MACHINES = 50
POLL_SECONDS = 10
SEED_HISTORY_MINUTES = 30


# ============================================================
# MACHINE
# ============================================================


def machine_code(number: int) -> str:
    return f"MACHINE-{number:02d}"


def ensure_machines() -> list[Machine]:
    """Tạo MACHINE-01 ... MACHINE-50 và tắt mock stress cũ nếu có."""

    wanted_codes = [
        machine_code(number)
        for number in range(1, TOTAL_MACHINES + 1)
    ]

    # Tắt các mã stress cũ dạng MACHINE-001 ... MACHINE-100 nếu còn.
    old_3_digit_codes = [
        f"MACHINE-{number:03d}"
        for number in range(1, 101)
    ]
    Machine.objects.filter(code__in=old_3_digit_codes).update(
        is_active=False
    )

    # Tắt các MACHINE-51 ... MACHINE-99 nếu đã từng tạo.
    extra_2_digit_codes = [
        machine_code(number)
        for number in range(TOTAL_MACHINES + 1, 100)
    ]
    Machine.objects.filter(code__in=extra_2_digit_codes).update(
        is_active=False
    )

    for number, code in enumerate(wanted_codes, start=1):
        group = number % 10

        descriptions = {
            1: "RUN ổn định / AUTO.",
            2: "STOP cố định / AUTO.",
            3: "ALARM 301 cố định.",
            4: "RUN ổn định / MANUAL.",
            5: "RUN với Cycle Time dao động.",
            6: "Mất dữ liệu định kỳ để test OFFLINE.",
            7: "Luân phiên RUN / STOP.",
            8: "RUN -> ALARM 702 -> STOP.",
            9: "RUN và thay đổi Recipe.",
            0: "Không RUN/STOP/ALARM để test UNKNOWN.",
        }

        Machine.objects.update_or_create(
            code=code,
            defaults={
                "name": f"Máy mô phỏng {number:02d}",
                "description": descriptions[group],
                "is_active": True,
            },
        )

    machines = list(
        Machine.objects.filter(code__in=wanted_codes)
        .order_by("code")
    )

    active_count = Machine.objects.filter(is_active=True).count()

    print(
        f"MOCK READY: {len(machines)} máy test | "
        f"ACTIVE toàn DB: {active_count}"
    )

    if active_count != TOTAL_MACHINES:
        print(
            "LƯU Ý: DB còn máy active ngoài bộ MACHINE-01..50. "
            "Dashboard có thể hiện nhiều hơn 50 máy."
        )

    return machines


# ============================================================
# COUNTER
# ============================================================


def latest_count(machine: Machine, fallback: int) -> int:
    value = (
        machine.readings
        .order_by("-recorded_at")
        .values_list("production_count", flat=True)
        .first()
    )

    return fallback if value is None else int(value)


# ============================================================
# SCENARIO
# ============================================================


def scenario_for(number: int, moment) -> dict:
    """Sinh trạng thái cho 10 nhóm, mỗi nhóm lặp lại 5 máy."""

    # Offset để 50 máy không đổi trạng thái cùng một thời điểm.
    seconds = int(moment.timestamp()) + number * 7
    group = number % 10

    if group == 1:
        return {
            "write": True,
            "label": "RUN-AUTO",
            "run_bit": True,
            "stop_bit": False,
            "alarm_bit": False,
            "auto_mode_bit": True,
            "cycle_time_ms": random.randint(3200, 3800),
            "alarm_code": 0,
            "recipe_no": 1,
        }

    if group == 2:
        return {
            "write": True,
            "label": "STOP",
            "run_bit": False,
            "stop_bit": True,
            "alarm_bit": False,
            "auto_mode_bit": True,
            "cycle_time_ms": None,
            "alarm_code": 0,
            "recipe_no": 2,
        }

    if group == 3:
        return {
            "write": True,
            "label": "ALARM-301",
            "run_bit": False,
            "stop_bit": False,
            "alarm_bit": True,
            "auto_mode_bit": True,
            "cycle_time_ms": None,
            "alarm_code": 301,
            "recipe_no": 3,
        }

    if group == 4:
        return {
            "write": True,
            "label": "RUN-MANUAL",
            "run_bit": True,
            "stop_bit": False,
            "alarm_bit": False,
            "auto_mode_bit": False,
            "cycle_time_ms": random.randint(4000, 4900),
            "alarm_code": 0,
            "recipe_no": 1,
        }

    if group == 5:
        slow = (seconds % 60) >= 40
        return {
            "write": True,
            "label": "RUN-SLOW" if slow else "RUN-NORMAL",
            "run_bit": True,
            "stop_bit": False,
            "alarm_bit": False,
            "auto_mode_bit": True,
            "cycle_time_ms": (
                random.randint(5000, 6200)
                if slow
                else random.randint(2900, 3500)
            ),
            "alarm_code": 0,
            "recipe_no": 2,
        }

    if group == 6:
        # 30 giây ghi dữ liệu, 30 giây không ghi.
        # STALE_AFTER_SECONDS=15 sẽ làm dashboard chuyển OFFLINE.
        write = (seconds % 60) < 30

        if not write:
            return {
                "write": False,
                "label": "NO-DATA",
            }

        return {
            "write": True,
            "label": "RUN-BEFORE-OFFLINE",
            "run_bit": True,
            "stop_bit": False,
            "alarm_bit": False,
            "auto_mode_bit": True,
            "cycle_time_ms": random.randint(3500, 4300),
            "alarm_code": 0,
            "recipe_no": 3,
        }

    if group == 7:
        running = (seconds % 40) < 20
        return {
            "write": True,
            "label": "RUN" if running else "STOP",
            "run_bit": running,
            "stop_bit": not running,
            "alarm_bit": False,
            "auto_mode_bit": True,
            "cycle_time_ms": (
                random.randint(3300, 4100)
                if running
                else None
            ),
            "alarm_code": 0,
            "recipe_no": 4,
        }

    if group == 8:
        phase = seconds % 75

        if phase < 25:
            return {
                "write": True,
                "label": "RUN",
                "run_bit": True,
                "stop_bit": False,
                "alarm_bit": False,
                "auto_mode_bit": True,
                "cycle_time_ms": random.randint(3400, 4200),
                "alarm_code": 0,
                "recipe_no": 4,
            }

        if phase < 50:
            return {
                "write": True,
                "label": "ALARM-702",
                "run_bit": False,
                "stop_bit": False,
                "alarm_bit": True,
                "auto_mode_bit": True,
                "cycle_time_ms": None,
                "alarm_code": 702,
                "recipe_no": 4,
            }

        return {
            "write": True,
            "label": "STOP",
            "run_bit": False,
            "stop_bit": True,
            "alarm_bit": False,
            "auto_mode_bit": True,
            "cycle_time_ms": None,
            "alarm_code": 0,
            "recipe_no": 4,
        }

    if group == 9:
        recipe = ((seconds // 40) % 4) + 1
        return {
            "write": True,
            "label": f"RUN-RECIPE-{recipe}",
            "run_bit": True,
            "stop_bit": False,
            "alarm_bit": False,
            "auto_mode_bit": True,
            "cycle_time_ms": random.randint(3000, 3900),
            "alarm_code": 0,
            "recipe_no": recipe,
        }

    # group == 0
    return {
        "write": True,
        "label": "UNKNOWN",
        "run_bit": False,
        "stop_bit": False,
        "alarm_bit": False,
        "auto_mode_bit": False,
        "cycle_time_ms": None,
        "alarm_code": 0,
        "recipe_no": 0,
    }


# ============================================================
# READING
# ============================================================


def make_reading(
    machine: Machine,
    number: int,
    moment,
    counter: int,
):
    scenario = scenario_for(number, moment)

    if not scenario["write"]:
        return None, counter, scenario["label"]

    if scenario["run_bit"] and not scenario["alarm_bit"]:
        counter += random.randint(1, 3)

    reading = MachineReading(
        machine=machine,
        recorded_at=moment,
        plc_online=False,
        run_bit=scenario["run_bit"],
        stop_bit=scenario["stop_bit"],
        alarm_bit=scenario["alarm_bit"],
        auto_mode_bit=scenario["auto_mode_bit"],
        production_count=counter,
        cycle_time_ms=scenario["cycle_time_ms"],
        alarm_code=scenario["alarm_code"],
        recipe_no=scenario["recipe_no"],
        source=MachineReading.DataSource.MOCK,
    )

    return reading, counter, scenario["label"]


# ============================================================
# HISTORY SEED
# ============================================================


def seed_recent_history(
    machines: list[Machine],
    counters: dict[int, int],
):
    """Tạo tối đa 30 phút lịch sử để Timeline có dữ liệu ngay."""

    now = timezone.now()
    since = now - timedelta(minutes=SEED_HISTORY_MINUTES)

    recent_machine_ids = set(
        MachineReading.objects.filter(
            machine__in=machines,
            recorded_at__gte=since,
        ).values_list("machine_id", flat=True)
    )

    rows: list[MachineReading] = []

    for number, machine in enumerate(machines, start=1):
        if machine.id in recent_machine_ids:
            continue

        counter = counters[machine.id]

        for minutes_ago in range(
            SEED_HISTORY_MINUTES,
            0,
            -1,
        ):
            moment = now - timedelta(minutes=minutes_ago)
            reading, counter, _label = make_reading(
                machine,
                number,
                moment,
                counter,
            )

            if reading is not None:
                rows.append(reading)

        counters[machine.id] = counter

    if rows:
        MachineReading.objects.bulk_create(
            rows,
            batch_size=500,
        )
        print(f"SEED: {len(rows)} bản ghi lịch sử.")
    else:
        print("SEED: đã có dữ liệu gần đây, bỏ qua.")


# ============================================================
# LIVE BATCH
# ============================================================


def build_live_batch(
    machines: list[Machine],
    counters: dict[int, int],
):
    moment = timezone.now()
    rows: list[MachineReading] = []

    stats = {
        "RUN": 0,
        "STOP": 0,
        "ALARM": 0,
        "NO-DATA": 0,
        "UNKNOWN": 0,
    }

    for number, machine in enumerate(machines, start=1):
        reading, counter, label = make_reading(
            machine,
            number,
            moment,
            counters[machine.id],
        )

        counters[machine.id] = counter

        if reading is None:
            stats["NO-DATA"] += 1
            continue

        rows.append(reading)

        if reading.alarm_bit:
            stats["ALARM"] += 1
        elif reading.run_bit:
            stats["RUN"] += 1
        elif reading.stop_bit:
            stats["STOP"] += 1
        else:
            stats["UNKNOWN"] += 1

    return rows, stats


# ============================================================
# MAIN
# ============================================================


def main():
    print("=" * 72)
    print("MACHINE MONITORING - MOCK 50 MACHINES")
    print(f"POLL: {POLL_SECONDS}s | HISTORY: {SEED_HISTORY_MINUTES} phút")
    print("KHÔNG KẾT NỐI PLC THẬT")
    print("=" * 72)

    while True:
        try:
            close_old_connections()

            machines = ensure_machines()

            counters = {
                machine.id: latest_count(
                    machine,
                    fallback=1000 + index * 100,
                )
                for index, machine in enumerate(
                    machines,
                    start=1,
                )
            }

            seed_recent_history(machines, counters)
            break

        except (OperationalError, InterfaceError) as exc:
            print(f"DB INIT ERROR: {exc}")
            print("Thử lại sau 10 giây...")
            close_old_connections()
            time.sleep(10)

    try:
        while True:
            cycle_started = time.monotonic()

            try:
                close_old_connections()

                rows, stats = build_live_batch(
                    machines,
                    counters,
                )

                db_started = time.monotonic()

                if rows:
                    with transaction.atomic():
                        MachineReading.objects.bulk_create(
                            rows,
                            batch_size=100,
                        )

                db_ms = int(
                    (time.monotonic() - db_started) * 1000
                )

                close_old_connections()

                print(
                    f"[{timezone.localtime():%H:%M:%S}] "
                    f"WRITE={len(rows):02d} | "
                    f"RUN={stats['RUN']:02d} | "
                    f"STOP={stats['STOP']:02d} | "
                    f"ALARM={stats['ALARM']:02d} | "
                    f"OFFLINE-WAIT={stats['NO-DATA']:02d} | "
                    f"UNKNOWN={stats['UNKNOWN']:02d} | "
                    f"DB={db_ms}ms"
                )

            except (OperationalError, InterfaceError) as exc:
                print(f"DB ERROR: {exc}")
                close_old_connections()
                print("Collector không crash; thử lại sau 10 giây.")
                time.sleep(10)
                continue

            elapsed = time.monotonic() - cycle_started
            sleep_for = max(0.0, POLL_SECONDS - elapsed)
            time.sleep(sleep_for)

    except KeyboardInterrupt:
        print("\nĐã dừng MOCK Collector.")

    finally:
        close_old_connections()


if __name__ == "__main__":
    main()