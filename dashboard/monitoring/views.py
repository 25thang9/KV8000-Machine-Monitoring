import csv
from dataclasses import dataclass
from datetime import timedelta

from django.core.paginator import Paginator
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_date

from .models import Machine, MachineReading


STALE_AFTER_SECONDS = 15

PLC_MAPPING = [
    {
        "address": "MR100",
        "data_type": "BIT",
        "name": "Máy chạy",
        "technical_name": "Machine Run",
        "field": "run_bit",
        "unit": "ON/OFF",
    },
    {
        "address": "MR101",
        "data_type": "BIT",
        "name": "Máy dừng",
        "technical_name": "Machine Stop",
        "field": "stop_bit",
        "unit": "ON/OFF",
    },
    {
        "address": "MR102",
        "data_type": "BIT",
        "name": "Cảnh báo",
        "technical_name": "Alarm",
        "field": "alarm_bit",
        "unit": "ON/OFF",
    },
    {
        "address": "MR103",
        "data_type": "BIT",
        "name": "Chế độ tự động",
        "technical_name": "Auto Mode",
        "field": "auto_mode_bit",
        "unit": "ON/OFF",
    },
    {
        "address": "DM1000",
        "data_type": "WORD / DWORD",
        "name": "Sản lượng",
        "technical_name": "Production Count",
        "field": "production_count",
        "unit": "sản phẩm",
    },
    {
        "address": "DM1002",
        "data_type": "WORD / DWORD",
        "name": "Thời gian chu kỳ",
        "technical_name": "Cycle Time",
        "field": "cycle_time_ms",
        "unit": "ms",
    },
    {
        "address": "DM1004",
        "data_type": "WORD",
        "name": "Mã cảnh báo",
        "technical_name": "Alarm Code",
        "field": "alarm_code",
        "unit": "mã",
    },
    {
        "address": "DM1005",
        "data_type": "WORD",
        "name": "Mã sản phẩm / Recipe",
        "technical_name": "Recipe / Product No.",
        "field": "recipe_no",
        "unit": "mã",
    },
]


@dataclass
class MachineSnapshot:
    machine: Machine
    latest: MachineReading | None
    state: str
    state_label: str
    connection: str
    connection_label: str
    is_stale: bool
    age_seconds: int | None


def _active_machines():
    return Machine.objects.filter(is_active=True).order_by("code")


def _select_machine(request):
    machines = list(_active_machines())
    selected_code = (
        request.GET.get("machine")
        or request.resolver_match.kwargs.get("code", "")
        if request.resolver_match
        else request.GET.get("machine", "")
    )
    selected_code = (selected_code or "").strip()

    machine = next(
        (item for item in machines if item.code == selected_code),
        machines[0] if machines else None,
    )
    return machines, machine


def _latest_reading(machine):
    if not machine:
        return None
    return machine.readings.order_by("-recorded_at").first()


def _snapshot(machine, latest=None):
    latest = latest or _latest_reading(machine)

    if latest is None:
        return MachineSnapshot(
            machine=machine,
            latest=None,
            state="unknown",
            state_label="CHƯA CÓ DỮ LIỆU",
            connection="offline",
            connection_label="Chưa có dữ liệu",
            is_stale=True,
            age_seconds=None,
        )

    age = max(
        0,
        int((timezone.now() - latest.recorded_at).total_seconds()),
    )
    is_stale = age > STALE_AFTER_SECONDS

    if is_stale:
        state = "offline"
        state_label = "DỮ LIỆU GIÁN ĐOẠN"
    elif latest.alarm_bit:
        state = "alarm"
        state_label = "CÓ CẢNH BÁO"
    elif latest.run_bit:
        state = "run"
        state_label = "ĐANG CHẠY"
    elif latest.stop_bit:
        state = "stop"
        state_label = "ĐANG DỪNG"
    else:
        state = "unknown"
        state_label = "CHƯA XÁC ĐỊNH"

    if latest.source == "MOCK":
        connection = "simulation"
        connection_label = "Dữ liệu mô phỏng"
    elif is_stale or not latest.plc_online:
        connection = "offline"
        connection_label = "PLC mất kết nối"
    else:
        connection = "online"
        connection_label = "PLC trực tuyến"

    return MachineSnapshot(
        machine=machine,
        latest=latest,
        state=state,
        state_label=state_label,
        connection=connection,
        connection_label=connection_label,
        is_stale=is_stale,
        age_seconds=age,
    )


def _signal_rows(latest):
    if latest is None:
        return []

    rows = []
    for item in PLC_MAPPING:
        value = getattr(latest, item["field"], None)
        if item["data_type"] == "BIT":
            display_value = "ON" if value else "OFF"
            value_class = "active" if value else "inactive"
        elif value is None:
            display_value = "—"
            value_class = "inactive"
        else:
            display_value = f"{value} {item['unit']}"
            value_class = "numeric"

        rows.append(
            {
                **item,
                "raw_value": value,
                "display_value": display_value,
                "value_class": value_class,
            }
        )
    return rows


def _query_readings(machine, request):
    readings = MachineReading.objects.select_related("machine").none()
    if machine:
        readings = (
            MachineReading.objects.select_related("machine")
            .filter(machine=machine)
            .order_by("-recorded_at")
        )

    status_filter = request.GET.get("status", "").strip().upper()
    source_filter = request.GET.get("source", "").strip().upper()
    date_from_value = request.GET.get("date_from", "").strip()
    date_to_value = request.GET.get("date_to", "").strip()

    if source_filter in {"MOCK", "PLC"}:
        readings = readings.filter(source=source_filter)

    if status_filter == "RUN":
        readings = readings.filter(alarm_bit=False, run_bit=True)
    elif status_filter == "STOP":
        readings = readings.filter(
            alarm_bit=False,
            run_bit=False,
            stop_bit=True,
        )
    elif status_filter == "ALARM":
        readings = readings.filter(alarm_bit=True)
    elif status_filter == "OFFLINE":
        readings = readings.filter(source="PLC", plc_online=False)

    date_from = parse_date(date_from_value)
    date_to = parse_date(date_to_value)
    if date_from:
        readings = readings.filter(recorded_at__date__gte=date_from)
    if date_to:
        readings = readings.filter(recorded_at__date__lte=date_to)

    return readings, {
        "selected_status": status_filter,
        "selected_source": source_filter,
        "date_from_value": date_from_value,
        "date_to_value": date_to_value,
    }


def dashboard(request):
    """Tổng quan nhiều máy, ưu tiên bất thường và dữ liệu mới nhất."""
    machines = list(_active_machines())
    snapshots = [_snapshot(machine) for machine in machines]

    summary = {
        "total": len(snapshots),
        "run": sum(item.state == "run" for item in snapshots),
        "stop": sum(item.state == "stop" for item in snapshots),
        "alarm": sum(item.state == "alarm" for item in snapshots),
        "offline": sum(item.state == "offline" for item in snapshots),
    }

    active_alarms = [
        item for item in snapshots if item.state == "alarm"
    ]

    return render(
        request,
        "monitoring/dashboard.html",
        {
            "page_key": "overview",
            "page_title": "Tổng quan hệ thống",
            "page_eyebrow": "TÌNH TRẠNG VẬN HÀNH HIỆN TẠI",
            "machines": machines,
            "snapshots": snapshots,
            "summary": summary,
            "active_alarms": active_alarms,
            "current_time": timezone.localtime(),
            "auto_refresh_seconds": 5,
        },
    )


def machine_detail(request, code=None):
    """Màn hình vận hành và kỹ thuật của một máy."""
    machines, machine = _select_machine(request)
    latest = _latest_reading(machine)
    snapshot = _snapshot(machine, latest) if machine else None

    recent_readings = []
    chart_data = []
    if machine:
        recent_readings = list(
            machine.readings.order_by("-recorded_at")[:100]
        )
        chart_data = [
            {
                "time": timezone.localtime(item.recorded_at).strftime(
                    "%H:%M:%S"
                ),
                "cycle": item.cycle_time_ms,
                "production": item.production_count,
            }
            for item in reversed(recent_readings[:60])
        ]

    return render(
        request,
        "monitoring/machine_detail.html",
        {
            "page_key": "machine",
            "page_title": "Chi tiết máy",
            "page_eyebrow": "GIÁM SÁT MỘT MÁY",
            "machines": machines,
            "machine": machine,
            "latest": latest,
            "snapshot": snapshot,
            "signal_rows": _signal_rows(latest),
            "recent_readings": recent_readings[:20],
            "chart_data": chart_data,
            "current_time": timezone.localtime(),
            "auto_refresh_seconds": 5,
        },
    )


def alarms(request):
    """Tổng hợp cảnh báo từ dữ liệu đọc máy, không thay đổi model."""
    machines, machine = _select_machine(request)

    readings = MachineReading.objects.select_related("machine")
    if machine:
        readings = readings.filter(machine=machine)
    readings = list(
        readings.filter(alarm_bit=True)
        .order_by("-recorded_at")[:1000]
    )
    readings.reverse()

    events = []
    for reading in readings:
        local_time = timezone.localtime(reading.recorded_at)
        previous = events[-1] if events else None
        same_event = (
            previous
            and previous["machine_code"] == reading.machine.code
            and previous["alarm_code"] == reading.alarm_code
            and local_time - previous["ended_at"] <= timedelta(seconds=10)
        )

        if same_event:
            previous["ended_at"] = local_time
            previous["samples"] += 1
        else:
            events.append(
                {
                    "machine_code": reading.machine.code,
                    "machine_name": reading.machine.name,
                    "alarm_code": reading.alarm_code,
                    "started_at": local_time,
                    "ended_at": local_time,
                    "samples": 1,
                    "source": reading.source,
                }
            )

    events.reverse()

    current_active = []
    for item in machines:
        latest = _latest_reading(item)
        if latest and latest.alarm_bit:
            current_active.append(_snapshot(item, latest))

    paginator = Paginator(events, 25)
    page_obj = paginator.get_page(request.GET.get("page"))

    return render(
        request,
        "monitoring/alarms.html",
        {
            "page_key": "alarms",
            "page_title": "Cảnh báo",
            "page_eyebrow": "ƯU TIÊN SỰ CỐ CẦN CHÚ Ý",
            "machines": machines,
            "machine": machine,
            "current_active": current_active,
            "page_obj": page_obj,
            "current_time": timezone.localtime(),
            "auto_refresh_seconds": 10,
        },
    )


def history(request):
    """Tra cứu, phân trang và xuất CSV dữ liệu máy."""
    machines, machine = _select_machine(request)
    readings, filter_context = _query_readings(machine, request)

    if request.GET.get("export") == "csv":
        response = HttpResponse(content_type="text/csv; charset=utf-8")
        code = machine.code if machine else "machine"
        response["Content-Disposition"] = (
            f'attachment; filename="{code}_history.csv"'
        )
        response.write("\ufeff")
        writer = csv.writer(response)
        writer.writerow(
            [
                "Thời gian",
                "Mã máy",
                "Trạng thái",
                "Chế độ",
                "Sản lượng",
                "Cycle Time (ms)",
                "Mã cảnh báo",
                "Recipe",
                "Nguồn",
            ]
        )
        for item in readings.iterator():
            writer.writerow(
                [
                    timezone.localtime(item.recorded_at).strftime(
                        "%d/%m/%Y %H:%M:%S"
                    ),
                    item.machine.code,
                    item.status_label,
                    "Tự động" if item.auto_mode_bit else "Thủ công",
                    item.production_count,
                    item.cycle_time_ms or "",
                    item.alarm_code,
                    item.recipe_no,
                    item.source,
                ]
            )
        return response

    page_size_raw = request.GET.get("page_size", "50")
    page_size = int(page_size_raw) if page_size_raw in {"25", "50", "100"} else 50

    total_records = readings.count()
    counters = {
        "total": total_records,
        "run": readings.filter(alarm_bit=False, run_bit=True).count(),
        "stop": readings.filter(
            alarm_bit=False,
            run_bit=False,
            stop_bit=True,
        ).count(),
        "alarm": readings.filter(alarm_bit=True).count(),
    }

    paginator = Paginator(readings, page_size)
    page_obj = paginator.get_page(request.GET.get("page"))

    query_parameters = request.GET.copy()
    for key in ("page", "export"):
        query_parameters.pop(key, None)

    return render(
        request,
        "monitoring/history.html",
        {
            "page_key": "history",
            "page_title": "Lịch sử và báo cáo",
            "page_eyebrow": "TRA CỨU DỮ LIỆU ĐÃ LƯU",
            "machines": machines,
            "machine": machine,
            "page_obj": page_obj,
            "page_size": page_size,
            "counters": counters,
            "query_string": query_parameters.urlencode(),
            "current_time": timezone.localtime(),
            **filter_context,
        },
    )


def system_status(request):
    """Thông tin cấu hình đọc-only; cấu hình thật quản lý ở .env/admin."""
    machines = list(_active_machines())
    snapshots = [_snapshot(machine) for machine in machines]

    return render(
        request,
        "monitoring/system.html",
        {
            "page_key": "system",
            "page_title": "Hệ thống",
            "page_eyebrow": "CẤU HÌNH VÀ CHẨN ĐOÁN",
            "machines": machines,
            "snapshots": snapshots,
            "mapping": PLC_MAPPING,
            "current_time": timezone.localtime(),
        },
    )


def legacy_live_data(request):
    code = request.GET.get("machine", "").strip()
    if not code:
        first = _active_machines().first()
        code = first.code if first else ""
    return redirect("monitoring:machine_detail", code=code) if code else redirect("monitoring:dashboard")


def legacy_plc_signals(request):
    return legacy_live_data(request)
