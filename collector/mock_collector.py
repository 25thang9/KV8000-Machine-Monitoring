"""
Mock Collector cho KV8000 Machine Monitoring.

Công dụng:
- Mô phỏng dữ liệu của máy khi chưa có PLC thật.
- Tạo trạng thái RUN, STOP và ALARM.
- Tăng bộ đếm sản lượng khi máy RUN.
- Ghi dữ liệu vào PostgreSQL.
- Giúp kiểm tra Dashboard, biểu đồ và lịch sử.

Khi kết nối PLC thật, file này sẽ được thay bằng
Collector đọc dữ liệu KEYENCE KV-8000.
"""

import os
import random
import sys
import time
from pathlib import Path


# ---------------------------------------------------------
# NẠP CẤU HÌNH DJANGO
# ---------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parents[1]

DASHBOARD_DIR = PROJECT_ROOT / "dashboard"

sys.path.insert(
    0,
    str(DASHBOARD_DIR),
)

os.environ.setdefault(
    "DJANGO_SETTINGS_MODULE",
    "machine_monitoring.settings",
)

import django

django.setup()


# Chỉ import Model sau khi Django đã được khởi tạo.
from django.db import OperationalError
from django.db import close_old_connections
from django.utils import timezone

from monitoring.models import Machine
from monitoring.models import MachineReading


# ---------------------------------------------------------
# CẤU HÌNH MÔ PHỎNG
# ---------------------------------------------------------

MACHINE_CODE = "MACHINE-01"
MACHINE_NAME = "Máy mô phỏng số 01"

# Poll Interval = chu kỳ giữa hai lần thu thập dữ liệu.
POLL_INTERVAL_SECONDS = 2

# Seed giúp dữ liệu ngẫu nhiên ổn định hơn khi kiểm thử.
RANDOM_SEED = 8000


def determine_machine_state(step_number):
    """
    Tạo chu kỳ hoạt động có thể quan sát được.

    Mỗi chu kỳ 30 bước:
    - Phần lớn thời gian: RUN.
    - Bước 10 đến 12: STOP.
    - Bước 24 đến 25: ALARM.
    """

    position = step_number % 30

    if position in {24, 25}:
        return "ALARM"

    if position in {10, 11, 12}:
        return "STOP"

    return "RUN"


def build_reading_values(
    machine_state,
    production_count,
    random_generator,
):
    """
    Chuyển trạng thái máy thành các giá trị tương ứng
    với địa chỉ PLC trong đề bài.
    """

    run_bit = machine_state == "RUN"
    stop_bit = machine_state == "STOP"
    alarm_bit = machine_state == "ALARM"

    cycle_time_ms = None
    alarm_code = 0

    if run_bit:
        production_count += 1

        cycle_time_ms = random_generator.randint(
            3200,
            4100,
        )

    elif alarm_bit:
        alarm_code = 101

    return {
        "production_count": production_count,
        "run_bit": run_bit,
        "stop_bit": stop_bit,
        "alarm_bit": alarm_bit,
        "cycle_time_ms": cycle_time_ms,
        "alarm_code": alarm_code,
    }


def print_reading(reading):
    """
    In kết quả ra Terminal để người lập trình
    quan sát Collector đang hoạt động.
    """

    local_time = timezone.localtime(
        reading.recorded_at
    )

    cycle_text = (
        f"{reading.cycle_time_ms} ms"
        if reading.cycle_time_ms is not None
        else "—"
    )

    print(
        f"[{local_time:%H:%M:%S}] "
        f"{reading.status_label:<7} | "
        f"Sản lượng: {reading.production_count:<6} | "
        f"Chu kỳ: {cycle_text:<8} | "
        f"Alarm: {reading.alarm_code}",
        flush=True,
    )


def main():
    """
    Chạy vòng lặp Collector cho đến khi người dùng
    nhấn Ctrl + C.
    """

    random_generator = random.Random(
        RANDOM_SEED
    )

    machine, _ = Machine.objects.get_or_create(
        code=MACHINE_CODE,
        defaults={
            "name": MACHINE_NAME,
            "description": (
                "Máy dùng để kiểm thử hệ thống "
                "trước khi kết nối PLC thật."
            ),
        },
    )

    latest_reading = (
        machine.readings
        .order_by("-recorded_at")
        .first()
    )

    production_count = (
        latest_reading.production_count
        if latest_reading
        else 0
    )

    step_number = 0

    print("=" * 72)
    print("KV8000 MACHINE MONITORING — MOCK COLLECTOR")
    print("=" * 72)
    print(f"Máy: {machine.code} — {machine.name}")
    print(
        "Nguồn: MOCK — dữ liệu mô phỏng, "
        "không phải PLC thật."
    )
    print(
        f"Chu kỳ ghi dữ liệu: "
        f"{POLL_INTERVAL_SECONDS} giây"
    )
    print("Nhấn Ctrl + C để dừng.")
    print("-" * 72)

    try:
        while True:
            machine_state = determine_machine_state(
                step_number
            )

            values = build_reading_values(
                machine_state,
                production_count,
                random_generator,
            )

            production_count = values[
                "production_count"
            ]

            # Đóng kết nối cũ nếu database đã restart.
            close_old_connections()

            try:
                reading = MachineReading.objects.create(
                    machine=machine,

                    # Không giả vờ rằng PLC thật đang online.
                    plc_online=False,

                    run_bit=values["run_bit"],
                    stop_bit=values["stop_bit"],
                    alarm_bit=values["alarm_bit"],

                    # Mô phỏng máy đang ở chế độ tự động.
                    auto_mode_bit=True,

                    production_count=production_count,

                    cycle_time_ms=values[
                        "cycle_time_ms"
                    ],

                    alarm_code=values["alarm_code"],

                    # Đổi Recipe sau mỗi 20 lần ghi.
                    recipe_no=(
                        1
                        + (
                            step_number // 20
                        ) % 3
                    ),

                    source=(
                        MachineReading
                        .DataSource
                        .MOCK
                    ),
                )

                print_reading(reading)

                step_number += 1

                time.sleep(
                    POLL_INTERVAL_SECONDS
                )

            except OperationalError as error:
                print(
                    "[DATABASE ERROR] "
                    "Không ghi được dữ liệu. "
                    "Collector sẽ thử lại sau 3 giây.",
                    flush=True,
                )

                print(
                    f"Chi tiết: {error}",
                    flush=True,
                )

                time.sleep(3)

    except KeyboardInterrupt:
        print()
        print("-" * 72)
        print("Mock Collector đã được dừng an toàn.")


if __name__ == "__main__":
    main()