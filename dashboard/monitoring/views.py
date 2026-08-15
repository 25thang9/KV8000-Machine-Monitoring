import csv
import io
import json
import time
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.core.paginator import Paginator
from django.db import IntegrityError, close_old_connections, connections, transaction
from django.db.models import (
    Case,
    Count,
    IntegerField,
    Max,
    OuterRef,
    Q,
    Subquery,
    Value,
    When,
)
from django.db.models.functions import TruncMinute
from django.http import HttpResponse, JsonResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views.decorators.http import require_POST

from .forms import (
    MachineProvisionForm,
    PlcControllerForm,
    find_mapping_conflicts,
)
from .models import (
    Machine,
    MachineCurrentState,
    MachineReading,
    PlcController,
    SignalMapping,
)


STALE_AFTER_SECONDS = settings.MONITOR_STALE_AFTER_SECONDS
TIMELINE_MINUTES = 30

SIGNAL_META = {
    SignalMapping.Signal.RUN: {
        "technical_name": "Machine Run",
    },
    SignalMapping.Signal.STOP: {
        "technical_name": "Machine Stop",
    },
    SignalMapping.Signal.ALARM: {
        "technical_name": "Alarm",
    },
    SignalMapping.Signal.AUTO_MODE: {
        "technical_name": "Auto Mode",
    },
    SignalMapping.Signal.PRODUCTION_COUNT: {
        "technical_name": "Production Count",
    },
    SignalMapping.Signal.CYCLE_TIME_MS: {
        "technical_name": "Cycle Time",
    },
    SignalMapping.Signal.ALARM_CODE: {
        "technical_name": "Alarm Code",
    },
    SignalMapping.Signal.RECIPE_NO: {
        "technical_name": "Recipe / Product No.",
    },
}

SIGNAL_ORDER = [choice for choice, _label in SignalMapping.Signal.choices]



@dataclass
class MachineSnapshot:
    machine: Machine
    latest: MachineReading | MachineCurrentState | None
    state: str
    state_label: str
    connection: str
    connection_label: str
    is_stale: bool
    age_seconds: int | None


_NOT_PROVIDED = object()


def _configured_machines():
    """Tất cả Machine đã gắn PLC, kể cả machine đang tạm dừng giám sát."""
    return (
        Machine.objects
        .filter(controller__isnull=False)
        .select_related("controller", "current_state")
        .order_by("code")
    )


def _active_machines():
    """Machine đang được giám sát realtime, không N+1 query."""
    return _configured_machines().filter(is_active=True)


def _select_machine(request, *, include_inactive=False):
    machines = list(
        _configured_machines() if include_inactive else _active_machines()
    )

    selected_code = request.GET.get("machine", "").strip()
    if not selected_code and request.resolver_match:
        selected_code = (
            request.resolver_match.kwargs.get("code", "") or ""
        ).strip()

    machine = next(
        (item for item in machines if item.code == selected_code),
        machines[0] if machines else None,
    )
    return machines, machine


def _latest_reading(machine):
    if not machine:
        return None

    # Realtime dùng bảng current-state (1 row/machine). Fallback lịch sử giúp
    # upgrade từ bản cũ vẫn hiển thị dữ liệu trước poll đầu tiên.
    current = getattr(machine, "current_state", None)
    if current is not None:
        return current

    return (
        machine.readings
        .filter(source=MachineReading.DataSource.PLC)
        .order_by("-recorded_at", "-pk")
        .first()
    )


def _latest_readings_map(machines):
    """Current state cho realtime; fallback latest history khi chưa có state."""
    result = {}
    missing_ids = []
    for machine in machines:
        current = getattr(machine, "current_state", None)
        if current is not None:
            result[machine.id] = current
        else:
            missing_ids.append(machine.id)

    if not missing_ids:
        return result

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
        .filter(pk__in=missing_ids)
        .annotate(latest_reading_id=Subquery(latest_id))
        .values_list("pk", "latest_reading_id")
    )
    reading_ids = [reading_id for _, reading_id in pairs if reading_id]
    readings = {
        row.pk: row
        for row in MachineReading.objects.filter(pk__in=reading_ids)
    }
    for machine_id, reading_id in pairs:
        if reading_id in readings:
            result[machine_id] = readings[reading_id]
    return result


def _snapshot(machine, latest=_NOT_PROVIDED):
    if latest is _NOT_PROVIDED:
        latest = _latest_reading(machine)

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

    age_seconds = max(
        0,
        int((timezone.now() - latest.recorded_at).total_seconds()),
    )
    controller = getattr(machine, "controller", None)
    stale_limit = STALE_AFTER_SECONDS
    if controller is not None:
        stale_limit = max(
            stale_limit,
            int(controller.poll_interval_ms / 1000 * 3) + 1,
        )
    is_stale = age_seconds > stale_limit
    is_plc_offline = not latest.plc_online

    # Một quy tắc trạng thái duy nhất cho dữ liệu hiện tại:
    # OFFLINE -> ALARM -> RUN -> STOP -> UNKNOWN.
    if is_stale or is_plc_offline:
        state = "offline"
        state_label = "DỮ LIỆU GIÁN ĐOẠN"
    elif latest.alarm_bit:
        state = "alarm"
        state_label = "CÓ CẢNH BÁO"
    elif latest.run_bit and latest.stop_bit:
        # RUN và STOP cùng ON là tín hiệu bất thường, phải nổi bật như Alarm
        # thay vì âm thầm coi là RUN.
        state = "alarm"
        state_label = "TÍN HIỆU RUN/STOP BẤT THƯỜNG"
    elif latest.run_bit:
        state = "run"
        state_label = "ĐANG CHẠY"
    elif latest.stop_bit:
        state = "stop"
        state_label = "ĐANG DỪNG"
    else:
        state = "unknown"
        state_label = "CHƯA XÁC ĐỊNH"

    if is_stale or is_plc_offline:
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
        age_seconds=age_seconds,
    )


def _signal_rows(machine, latest):
    if latest is None or machine is None:
        return []

    mappings = {
        item.signal: item
        for item in machine.signal_mappings.filter(is_enabled=True)
    }
    rows = []
    for signal in SIGNAL_ORDER:
        mapping = mappings.get(signal)
        if mapping is None:
            continue

        value = getattr(latest, mapping.field_name, None)
        if mapping.data_type == SignalMapping.DataType.BIT:
            display_value = "ON" if value else "OFF"
            value_class = "active" if value else "inactive"
        elif value is None:
            display_value = "—"
            value_class = "inactive"
        else:
            display_value = f"{value} {mapping.unit}"
            value_class = "numeric"

        rows.append(
            {
                "address": mapping.address,
                "data_type": mapping.data_type,
                "name": mapping.get_signal_display(),
                "technical_name": SIGNAL_META.get(signal, {}).get(
                    "technical_name", signal
                ),
                "field": mapping.field_name,
                "unit": mapping.unit,
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
            MachineReading.objects
            .select_related("machine")
            .filter(
                machine=machine,
                source=MachineReading.DataSource.PLC,
            )
            .order_by("-recorded_at", "-pk")
        )

    status_filter = request.GET.get("status", "").strip().upper()
    date_from_value = request.GET.get("date_from", "").strip()
    date_to_value = request.GET.get("date_to", "").strip()

    if status_filter == "RUN":
        readings = readings.filter(
            plc_online=True,
            alarm_bit=False,
            run_bit=True,
            stop_bit=False,
        )
    elif status_filter == "STOP":
        readings = readings.filter(
            plc_online=True,
            alarm_bit=False,
            run_bit=False,
            stop_bit=True,
        )
    elif status_filter == "ALARM":
        readings = readings.filter(plc_online=True).filter(
            Q(alarm_bit=True) | Q(run_bit=True, stop_bit=True)
        )
    elif status_filter == "OFFLINE":
        # Đây là các sample PLC ghi nhận mất kết nối. Khoảng thời gian
        # hoàn toàn không có sample được thể hiện ở Timeline, không phải
        # một hàng riêng trong lịch sử thô.
        readings = readings.filter(
            plc_online=False,
        )

    date_from = parse_date(date_from_value)
    date_to = parse_date(date_to_value)

    if date_from:
        readings = readings.filter(recorded_at__date__gte=date_from)
    if date_to:
        readings = readings.filter(recorded_at__date__lte=date_to)

    return readings, {
        "selected_status": status_filter,
        "selected_source": "PLC",
        "date_from_value": date_from_value,
        "date_to_value": date_to_value,
    }


def _timeline_state(bucket):
    """Trạng thái đại diện cho một bucket 1 phút đã aggregate."""
    if bucket is None:
        return "offline", "Mất dữ liệu"
    if bucket["has_plc_offline"]:
        return "offline", "Mất dữ liệu"
    if bucket["has_alarm"]:
        return "alarm", "Cảnh báo"
    if bucket.get("has_abnormal"):
        return "alarm", "RUN/STOP bất thường"
    if bucket["has_run"]:
        return "run", "Đang chạy"
    if bucket["has_stop"]:
        return "stop", "Đang dừng"
    return "unknown", "Chưa xác định"


def _build_status_timeline(machines, minutes=TIMELINE_MINUTES):
    """
    Timeline 1 phút/bucket.

    Điểm quan trọng: aggregate trong DB, không tải toàn bộ raw readings
    của toàn bộ khoảng thời gian vào Python. Số hàng aggregate tăng theo
    số Machine đang cấu hình và số bucket thời gian.
    """
    now_local = timezone.localtime()
    start_local = (
        now_local.replace(second=0, microsecond=0)
        - timedelta(minutes=minutes - 1)
    )
    end_local = start_local + timedelta(minutes=minutes)

    machine_ids = [machine.id for machine in machines]
    grouped = {}

    if machine_ids:
        current_tz = timezone.get_current_timezone()

        bucket_rows = (
            MachineReading.objects
            .filter(
                machine_id__in=machine_ids,
                source=MachineReading.DataSource.PLC,
                recorded_at__gte=start_local,
                recorded_at__lt=end_local,
            )
            .annotate(
                bucket=TruncMinute(
                    "recorded_at",
                    tzinfo=current_tz,
                )
            )
            .values("machine_id", "bucket")
            .annotate(
                has_plc_offline=Max(
                    Case(
                        When(
                            plc_online=False,
                            then=Value(1),
                        ),
                        default=Value(0),
                        output_field=IntegerField(),
                    )
                ),
                has_alarm=Max(
                    Case(
                        When(alarm_bit=True, then=Value(1)),
                        default=Value(0),
                        output_field=IntegerField(),
                    )
                ),
                has_abnormal=Max(
                    Case(
                        When(run_bit=True, stop_bit=True, then=Value(1)),
                        default=Value(0),
                        output_field=IntegerField(),
                    )
                ),
                has_run=Max(
                    Case(
                        When(run_bit=True, then=Value(1)),
                        default=Value(0),
                        output_field=IntegerField(),
                    )
                ),
                has_stop=Max(
                    Case(
                        When(stop_bit=True, then=Value(1)),
                        default=Value(0),
                        output_field=IntegerField(),
                    )
                ),
            )
        )

        for row in bucket_rows:
            bucket_time = row["bucket"]
            if timezone.is_aware(bucket_time):
                bucket_time = timezone.localtime(bucket_time)
            bucket_time = bucket_time.replace(second=0, microsecond=0)
            grouped[(row["machine_id"], bucket_time)] = row

    timeline_rows = []

    for machine in machines:
        segments = []

        for index in range(minutes):
            bucket_time = start_local + timedelta(minutes=index)
            state, label = _timeline_state(
                grouped.get((machine.id, bucket_time))
            )
            segments.append(
                {
                    "state": state,
                    "label": label,
                    "time": bucket_time.strftime("%H:%M"),
                }
            )

        timeline_rows.append(
            {
                "machine": machine,
                "segments": segments,
            }
        )

    timeline_labels = {
        "start": start_local.strftime("%H:%M"),
        "middle": (
            start_local + timedelta(minutes=minutes // 2)
        ).strftime("%H:%M"),
        "end": now_local.strftime("%H:%M"),
    }

    return timeline_rows, timeline_labels



def _dashboard_state_payload():
    """Snapshot rất nhẹ dùng chung cho JSON fallback và SSE realtime."""
    machines = list(_active_machines())
    latest_by_machine = _latest_readings_map(machines)
    snapshots = [
        _snapshot(machine, latest_by_machine.get(machine.id))
        for machine in machines
    ]

    summary = {
        "total": len(snapshots),
        "run": sum(item.state == "run" for item in snapshots),
        "stop": sum(item.state == "stop" for item in snapshots),
        "alarm": sum(item.state == "alarm" for item in snapshots),
        "offline": sum(item.state == "offline" for item in snapshots),
        "unknown": sum(item.state == "unknown" for item in snapshots),
    }

    rows = []
    for item in snapshots:
        latest = item.latest
        rows.append(
            {
                "code": item.machine.code,
                "name": item.machine.name,
                "detail_url": f"/machines/{item.machine.code}/",
                "state": item.state,
                "state_label": item.state_label,
                "connection": item.connection,
                "connection_label": item.connection_label,
                "age_seconds": item.age_seconds,
                "recorded_at": (
                    timezone.localtime(latest.recorded_at).isoformat()
                    if latest is not None else None
                ),
                "run_bit": bool(latest.run_bit) if latest else False,
                "stop_bit": bool(latest.stop_bit) if latest else False,
                "alarm_bit": bool(latest.alarm_bit) if latest else False,
                "auto_mode_bit": bool(latest.auto_mode_bit) if latest else False,
                "production_count": latest.production_count if latest else None,
                "cycle_time_ms": latest.cycle_time_ms if latest else None,
                "alarm_code": latest.alarm_code if latest else None,
                "recipe_no": latest.recipe_no if latest else None,
                "plc_online": bool(latest.plc_online) if latest else False,
                "source": latest.source if latest else None,
            }
        )

    return {
        "ok": True,
        "generated_at": timezone.localtime().isoformat(),
        "summary": summary,
        "machines": rows,
    }


def _dashboard_state_signature(payload):
    """Signature bỏ age/generated_at để chỉ push ngay khi dữ liệu thật đổi."""
    compact = {
        "summary": payload["summary"],
        "machines": [
            {
                key: row.get(key)
                for key in (
                    "code",
                    "state",
                    "state_label",
                    "connection",
                    "run_bit",
                    "stop_bit",
                    "alarm_bit",
                    "auto_mode_bit",
                    "production_count",
                    "cycle_time_ms",
                    "alarm_code",
                    "recipe_no",
                    "plc_online",
                )
            }
            for row in payload["machines"]
        ],
    }
    return json.dumps(compact, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def dashboard_state_api(request):
    """JSON fallback nếu browser/proxy không giữ được SSE."""
    response = JsonResponse(_dashboard_state_payload())
    response["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response["Pragma"] = "no-cache"
    return response


def dashboard_state_stream(request):
    """Server-Sent Events: push thay đổi CurrentState đến browser gần realtime.

    Đây là kênh READ-ONLY từ database ra giao diện. Stream không ghi PLC.
    Heartbeat giữ kết nối qua WSGI/proxy và cập nhật tuổi dữ liệu định kỳ.
    """

    def event_stream():
        last_signature = None
        last_send = 0.0
        yield "retry: 1000\n\n"

        while True:
            try:
                close_old_connections()
                payload = _dashboard_state_payload()
                signature = _dashboard_state_signature(payload)

                # SSE là request sống lâu. Không giữ một connection PostgreSQL
                # riêng cho từng tab trình duyệt trong suốt lifetime stream.
                # Đóng connection của thread ngay sau snapshot để tránh cạn
                # max_connections khi mở nhiều client/tabs trên LAN.
                connections.close_all()

                now_mono = time.monotonic()
                changed = signature != last_signature
                heartbeat_due = (now_mono - last_send) >= 5.0

                if changed or heartbeat_due:
                    data = json.dumps(
                        payload,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    yield f"event: state\ndata: {data}\n\n"
                    last_signature = signature
                    last_send = now_mono

                # Collector mặc định poll khoảng 1 s; 500 ms đủ realtime
                # mà tránh query DB vô ích 4 lần/giây cho mỗi browser.
                time.sleep(0.5)

            except GeneratorExit:
                break
            except (BrokenPipeError, ConnectionResetError):
                break
            except Exception as exc:
                # Giữ stream sống qua lỗi DB ngắn hạn; EventSource cũng tự reconnect.
                error = json.dumps(
                    {"message": str(exc)[:300]},
                    ensure_ascii=False,
                )
                try:
                    yield f"event: stream_error\ndata: {error}\n\n"
                except Exception:
                    break
                time.sleep(1.0)
            finally:
                # close_old_connections() chỉ đóng connection cũ/unusable;
                # với SSE cần giải phóng ngay connection của request thread.
                connections.close_all()

    response = StreamingHttpResponse(
        event_stream(),
        content_type="text/event-stream; charset=utf-8",
    )
    response["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response["X-Accel-Buffering"] = "no"
    return response

def dashboard(request):
    machines = list(_active_machines())
    latest_by_machine = _latest_readings_map(machines)

    snapshots = [
        _snapshot(machine, latest_by_machine.get(machine.id))
        for machine in machines
    ]

    summary = {
        "total": len(snapshots),
        "run": sum(item.state == "run" for item in snapshots),
        "stop": sum(item.state == "stop" for item in snapshots),
        "alarm": sum(item.state == "alarm" for item in snapshots),
        "offline": sum(item.state == "offline" for item in snapshots),
        "unknown": sum(item.state == "unknown" for item in snapshots),
    }

    state_priority = {
        "alarm": 0,
        "offline": 1,
        "stop": 2,
        "unknown": 3,
        "run": 4,
    }
    snapshots.sort(
        key=lambda item: (
            state_priority.get(item.state, 99),
            item.machine.code,
        )
    )

    active_alarms = [
        item for item in snapshots if item.state == "alarm"
    ]

    updating_count = sum(
        1
        for item in snapshots
        if item.latest and not item.is_stale
    )
    collector_health = {
        "total": len(snapshots),
        "updating": updating_count,
        "interrupted": len(snapshots) - updating_count,
    }
    collector_ok = bool(
        snapshots and updating_count == len(snapshots)
    )

    focus_snapshot = next(
        (item for item in snapshots if item.state == "run"),
        snapshots[0] if snapshots else None,
    )

    status_timeline, timeline_labels = _build_status_timeline(
        machines,
        minutes=TIMELINE_MINUTES,
    )

    recent_alarm_rows = list(
        MachineReading.objects
        .select_related("machine")
        .filter(
            machine__is_active=True,
            source=MachineReading.DataSource.PLC,
            plc_online=True,
            alarm_bit=True,
        )
        .order_by("-recorded_at", "-pk")[:5]
    )

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
            "focus_snapshot": focus_snapshot,
            "collector_ok": collector_ok,
            "collector_health": collector_health,
            "recent_alarm_rows": recent_alarm_rows,
            "status_timeline": status_timeline,
            "timeline_labels": timeline_labels,
            "current_time": timezone.localtime(),
            "auto_refresh_seconds": 0,
        },
    )


def machine_detail(request, code=None):
    machines, machine = _select_machine(request)
    latest = _latest_reading(machine)
    snapshot = _snapshot(machine, latest) if machine else None

    previous_machine = None
    next_machine = None
    current_machine_index = None

    if machine and machines:
        current_machine_index = next(
            (
                index
                for index, item in enumerate(machines)
                if item.code == machine.code
            ),
            None,
        )

        if current_machine_index is not None:
            if current_machine_index > 0:
                previous_machine = machines[current_machine_index - 1]
            if current_machine_index < len(machines) - 1:
                next_machine = machines[current_machine_index + 1]

    recent_readings = []
    chart_data = []
    last_cycle_time_ms = None

    if machine:
        recent_readings = list(
            machine.readings
            .filter(source=MachineReading.DataSource.PLC)
            .order_by("-recorded_at", "-pk")[:100]
        )

        cycle_readings = list(
            machine.readings
            .filter(source=MachineReading.DataSource.PLC)
            .exclude(cycle_time_ms__isnull=True)
            .order_by("-recorded_at", "-pk")[:60]
        )

        if cycle_readings:
            last_cycle_time_ms = cycle_readings[0].cycle_time_ms

        chart_data = [
            {
                "time": timezone.localtime(
                    item.recorded_at
                ).strftime("%H:%M:%S"),
                "cycle": item.cycle_time_ms,
                "production": item.production_count,
            }
            for item in reversed(cycle_readings)
        ]

    mapping_addresses = {}
    if machine:
        mapping_addresses = {
            mapping.field_name: mapping.address
            for mapping in machine.signal_mappings.filter(is_enabled=True)
        }

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
            "signal_rows": _signal_rows(machine, latest),
            "mapping_addresses": mapping_addresses,
            "recent_readings": recent_readings[:20],
            "chart_data": chart_data,
            "last_cycle_time_ms": last_cycle_time_ms,
            "previous_machine": previous_machine,
            "next_machine": next_machine,
            "current_machine_index": current_machine_index,
            "machine_count": len(machines),
            "current_time": timezone.localtime(),
            "auto_refresh_seconds": 0,
        },
    )


def _format_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def _build_alarm_events(readings):
    """Dựng sự kiện Alarm từ các transition đã lưu trong MachineReading.

    Collector ghi ngay khi alarm_bit/alarm_code thay đổi, vì vậy dùng transition
    ON -> OFF chính xác hơn việc ghép các sample alarm theo một "gap" cố định.
    """
    events = []
    active_by_machine = {}

    for reading in readings:
        local_time = timezone.localtime(reading.recorded_at)
        active = active_by_machine.get(reading.machine_id)
        alarm_on = bool(reading.plc_online and reading.alarm_bit)
        alarm_code = int(reading.alarm_code or 0)

        if alarm_on:
            if active is None or active["alarm_code"] != alarm_code:
                if active is not None:
                    active["ended_at"] = local_time
                    active["is_active"] = False
                    active["duration_seconds"] = max(
                        0,
                        int((local_time - active["started_at"]).total_seconds()),
                    )
                    active["duration_text"] = _format_duration(
                        active["duration_seconds"]
                    )

                active = {
                    "machine_code": reading.machine.code,
                    "machine_name": reading.machine.name,
                    "alarm_code": alarm_code,
                    "started_at": local_time,
                    "ended_at": None,
                    "last_seen_at": local_time,
                    "samples": 1,
                    "source": reading.source,
                    "is_active": True,
                    "duration_seconds": 0,
                    "duration_text": "00:00",
                }
                events.append(active)
                active_by_machine[reading.machine_id] = active
            else:
                active["last_seen_at"] = local_time
                active["samples"] += 1
        elif active is not None:
            # alarm_bit OFF hoặc PLC mất dữ liệu: từ thời điểm này không còn
            # bằng chứng rằng Alarm vẫn đang active.
            active["ended_at"] = local_time
            active["is_active"] = False
            active["duration_seconds"] = max(
                0,
                int((local_time - active["started_at"]).total_seconds()),
            )
            active["duration_text"] = _format_duration(
                active["duration_seconds"]
            )
            active_by_machine.pop(reading.machine_id, None)

    now_local = timezone.localtime()
    for active in active_by_machine.values():
        active["duration_seconds"] = max(
            0,
            int((now_local - active["started_at"]).total_seconds()),
        )
        active["duration_text"] = _format_duration(active["duration_seconds"])

    events.sort(key=lambda item: item["started_at"], reverse=True)
    return events


def alarms(request):
    # Lịch sử phải còn tra cứu được sau khi một Machine bị tạm dừng giám sát.
    # Chỉ phần "đang cảnh báo" mới giới hạn ở Machine active.
    machines = list(_configured_machines())
    selected_code = request.GET.get("machine", "").strip()

    machine = None
    if selected_code:
        machine = next(
            (item for item in machines if item.code == selected_code),
            None,
        )

    # Lấy cả sample Alarm ON và sample chuyển về OFF để xác định chính xác
    # thời điểm bắt đầu/kết thúc. Giới hạn theo số raw row để trang vẫn nhẹ.
    event_query = (
        MachineReading.objects
        .select_related("machine")
        .filter(source=MachineReading.DataSource.PLC)
    )
    if machine:
        event_query = event_query.filter(machine=machine)

    readings = list(
        event_query.order_by("-recorded_at", "-pk")[:5000]
    )
    readings.reverse()
    events = _build_alarm_events(readings)

    if machine is not None:
        target_machines = [machine] if machine.is_active else []
    else:
        target_machines = [item for item in machines if item.is_active]
    latest_by_machine = _latest_readings_map(target_machines)
    current_active = []

    for item in target_machines:
        snapshot = _snapshot(
            item,
            latest_by_machine.get(item.id),
        )
        if snapshot.state == "alarm":
            current_active.append(snapshot)

    page_obj = Paginator(events, 25).get_page(
        request.GET.get("page")
    )

    return render(
        request,
        "monitoring/alarms.html",
        {
            "page_key": "alarms",
            "page_title": "Cảnh báo",
            "page_eyebrow": "ƯU TIÊN SỰ CỐ CẦN CHÚ Ý",
            "machines": machines,
            "machine": machine,
            "selected_machine_code": selected_code,
            "current_active": current_active,
            "page_obj": page_obj,
            "current_time": timezone.localtime(),
            "auto_refresh_seconds": 10,
        },
    )


def history(request):
    # Tạm dừng giám sát không được làm mất quyền tra cứu lịch sử cũ.
    machines, machine = _select_machine(request, include_inactive=True)
    readings, filter_context = _query_readings(machine, request)

    if request.GET.get("export") == "csv":
        response = HttpResponse(
            content_type="text/csv; charset=utf-8"
        )
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

        for item in readings.iterator(chunk_size=1000):
            writer.writerow(
                [
                    timezone.localtime(
                        item.recorded_at
                    ).strftime("%d/%m/%Y %H:%M:%S"),
                    item.machine.code,
                    item.status_label,
                    "Tự động" if item.auto_mode_bit else "Thủ công",
                    item.production_count,
                    (
                        item.cycle_time_ms
                        if item.cycle_time_ms is not None
                        else ""
                    ),
                    item.alarm_code,
                    item.recipe_no,
                    item.source,
                ]
            )

        return response

    page_size_raw = request.GET.get("page_size", "50")
    page_size = (
        int(page_size_raw)
        if page_size_raw in {"25", "50", "100"}
        else 50
    )

    counters = readings.aggregate(
        total=Count("id"),
        run=Count(
            "id",
            filter=Q(plc_online=True, alarm_bit=False, run_bit=True, stop_bit=False),
        ),
        stop=Count(
            "id",
            filter=Q(
                plc_online=True,
                alarm_bit=False,
                run_bit=False,
                stop_bit=True,
            ),
        ),
        alarm=Count(
            "id",
            filter=Q(plc_online=True) & (
                Q(alarm_bit=True) | Q(run_bit=True, stop_bit=True)
            ),
        ),
    )

    page_obj = Paginator(readings, page_size).get_page(
        request.GET.get("page")
    )

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
    """Thông tin runtime read-only; cấu hình PLC/mapping quản lý ở Admin."""
    machines = list(_active_machines())
    latest_by_machine = _latest_readings_map(machines)
    snapshots = [
        _snapshot(machine, latest_by_machine.get(machine.id))
        for machine in machines
    ]
    controllers = list(
        PlcController.objects
        .prefetch_related("machines")
        .order_by("code")
    )
    mappings = list(
        SignalMapping.objects
        .select_related("machine", "machine__controller")
        .filter(machine__is_active=True, is_enabled=True)
        .order_by("machine__code", "signal")
    )

    return render(
        request,
        "monitoring/system.html",
        {
            "page_key": "system",
            "page_title": "Hệ thống",
            "page_eyebrow": "CẤU HÌNH VÀ CHẨN ĐOÁN",
            "machines": machines,
            "snapshots": snapshots,
            "controllers": controllers,
            "mappings": mappings,
            "stale_after_seconds": STALE_AFTER_SECONDS,
            "current_time": timezone.localtime(),
            "app_env": settings.APP_ENV,
            "db_engine": settings.DB_ENGINE,
        },
    )



CSV_MACHINE_HEADERS = [
    "machine_code",
    "machine_name",
    "plc_code",
    "run",
    "stop",
    "alarm",
    "auto",
    "production",
    "cycle",
    "alarm_code",
    "recipe",
    "description",
    "is_active",
    "production_type",
    "production_word_order",
    "cycle_type",
    "cycle_word_order",
    "alarm_code_type",
    "alarm_code_word_order",
    "recipe_type",
    "recipe_word_order",
]


def _csv_value(row, *names, default=""):
    for name in names:
        value = row.get(name)
        if value is not None and str(value).strip() != "":
            return str(value).strip()
    return default


def _csv_bool(value, *, default=False):
    if value is None or str(value).strip() == "":
        return default
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "y", "on", "active", "enable", "enabled"}:
        return True
    if normalized in {"0", "false", "no", "n", "off", "inactive", "disable", "disabled"}:
        return False
    raise ValueError("is_active chỉ nhận TRUE/FALSE, 1/0, YES/NO hoặc ON/OFF")


def _machine_form_data_from_csv(row, controllers_by_code):
    plc_code = _csv_value(row, "plc_code", "controller_code", "plc").upper()
    controller = controllers_by_code.get(plc_code)
    if controller is None:
        raise ValueError(f"PLC '{plc_code or '(trống)'}' chưa được cấu hình")

    uint16 = SignalMapping.DataType.UINT16
    low_high = SignalMapping.WordOrder.LOW_HIGH
    return {
        "machine_code": _csv_value(row, "machine_code", "code").upper(),
        "machine_name": _csv_value(row, "machine_name", "name"),
        "description": _csv_value(row, "description"),
        "controller": controller.pk,
        # Import hàng loạt mặc định PAUSED nếu cột is_active để trống. Người
        # cấu hình có thể kiểm tra mapping trước rồi bật, tránh đọc nhầm device.
        "is_active": _csv_bool(row.get("is_active"), default=False),
        "run_address": _csv_value(row, "run", "run_address").upper(),
        "stop_address": _csv_value(row, "stop", "stop_address").upper(),
        "alarm_address": _csv_value(row, "alarm", "alarm_address").upper(),
        "auto_address": _csv_value(row, "auto", "auto_address", "auto_mode").upper(),
        "production_address": _csv_value(
            row, "production", "production_address", "production_count"
        ).upper(),
        "production_type": _csv_value(row, "production_type", default=uint16).upper(),
        "production_word_order": _csv_value(
            row, "production_word_order", default=low_high
        ).upper(),
        "cycle_address": _csv_value(
            row, "cycle", "cycle_address", "cycle_time"
        ).upper(),
        "cycle_type": _csv_value(row, "cycle_type", default=uint16).upper(),
        "cycle_word_order": _csv_value(
            row, "cycle_word_order", default=low_high
        ).upper(),
        "alarm_code_address": _csv_value(
            row, "alarm_code", "alarm_code_address"
        ).upper(),
        "alarm_code_type": _csv_value(row, "alarm_code_type", default=uint16).upper(),
        "alarm_code_word_order": _csv_value(
            row, "alarm_code_word_order", default=low_high
        ).upper(),
        "recipe_address": _csv_value(
            row, "recipe", "recipe_address", "recipe_no", "product_no"
        ).upper(),
        "recipe_type": _csv_value(row, "recipe_type", default=uint16).upper(),
        "recipe_word_order": _csv_value(
            row, "recipe_word_order", default=low_high
        ).upper(),
    }


def _form_error_text(form):
    parts = []
    for field, errors in form.errors.items():
        label = "Cấu hình" if field == "__all__" else form.fields[field].label
        parts.extend(f"{label}: {error}" for error in errors)
    return "; ".join(parts)


def machine_import_template(request):
    """Tải CSV mẫu; không chứa dữ liệu thật và không thay đổi database."""
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="machine_mapping_template.csv"'
    response.write("\ufeff")
    writer = csv.writer(response)
    writer.writerow(CSV_MACHINE_HEADERS)
    writer.writerow(
        [
            "MACHINE06", "Machine 06", "PLC01",
            "MR600", "MR601", "MR602", "MR603",
            "DM6000", "DM6002", "DM6004", "DM6005",
            "", "FALSE",
            "UINT16", "LOW_HIGH",
            "UINT16", "LOW_HIGH",
            "UINT16", "LOW_HIGH",
            "UINT16", "LOW_HIGH",
        ]
    )
    return response


@require_POST
def import_machine_csv(request):
    """Tạo nhiều Machine + 8 mapping từ CSV theo cơ chế all-or-nothing."""
    upload = request.FILES.get("csv_file")
    if upload is None:
        messages.error(request, "Chưa chọn file CSV.")
        return redirect("monitoring:configuration")
    if upload.size > 2 * 1024 * 1024:
        messages.error(request, "CSV quá lớn. Giới hạn 2 MB.")
        return redirect("monitoring:configuration")

    try:
        raw = upload.read()
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("utf-8")

        sample = text[:4096]
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(io.StringIO(text), dialect=dialect)
        if not reader.fieldnames:
            raise ValueError("CSV không có dòng tiêu đề")

        normalized_headers = [str(name or "").strip().lower() for name in reader.fieldnames]
        if "machine_code" not in normalized_headers or "plc_code" not in normalized_headers:
            raise ValueError("CSV phải có tối thiểu cột machine_code và plc_code")

        controllers_by_code = {
            plc.code.strip().upper(): plc
            for plc in PlcController.objects.all()
        }
        pending_forms = []
        errors = []
        seen_codes = set()

        for row_number, raw_row in enumerate(reader, start=2):
            if row_number > 502:
                errors.append("Tối đa 500 dòng Machine trong một lần import.")
                break

            row = {
                str(key or "").strip().lower(): value
                for key, value in raw_row.items()
            }
            if not any(str(value or "").strip() for value in row.values()):
                continue

            try:
                data = _machine_form_data_from_csv(row, controllers_by_code)
            except ValueError as exc:
                errors.append(f"Dòng {row_number}: {exc}")
                continue

            code = data["machine_code"]
            if not code:
                errors.append(f"Dòng {row_number}: machine_code đang trống")
                continue
            if code in seen_codes:
                errors.append(f"Dòng {row_number}: machine_code {code} bị lặp trong CSV")
                continue
            seen_codes.add(code)

            form = MachineProvisionForm(data=data)
            if not form.is_valid():
                errors.append(f"Dòng {row_number}: {_form_error_text(form)}")
                continue
            pending_forms.append((row_number, form))

        if not pending_forms and not errors:
            errors.append("CSV không có dòng dữ liệu Machine nào.")

        if errors:
            preview = " | ".join(errors[:6])
            if len(errors) > 6:
                preview += f" | ... và {len(errors) - 6} lỗi khác"
            messages.error(
                request,
                "Không import dữ liệu. Không có Machine nào được tạo. " + preview,
            )
            return redirect("monitoring:configuration")

        try:
            with transaction.atomic():
                created = [form.save() for _row_number, form in pending_forms]
        except (IntegrityError, ValueError) as exc:
            messages.error(
                request,
                f"Import bị hủy và đã rollback toàn bộ dữ liệu: {exc}",
            )
            return redirect("monitoring:configuration")

        messages.success(
            request,
            f"Đã import {len(created)} Machine. "
            "Máy có is_active để trống/FALSE sẽ ở trạng thái PAUSED để kiểm tra mapping trước.",
        )
    except (csv.Error, ValueError, UnicodeError) as exc:
        messages.error(request, f"Không đọc được CSV: {exc}")

    return redirect("monitoring:configuration")


def configuration(request):
    """Quản lý PLC, Machine và SignalMapping trong database.

    Trang này không ghi device xuống PLC. Collector vẫn chỉ đọc Host Link TCP.
    Số lượng PLC/Machine không bị hard-code trong source.
    """
    edit_plc = None
    edit_machine = None
    clone_source = None

    plc_id = request.GET.get("edit_plc", "").strip()
    machine_id = request.GET.get("edit_machine", "").strip()
    clone_id = request.GET.get("clone_machine", "").strip()
    if plc_id.isdigit():
        edit_plc = get_object_or_404(PlcController, pk=int(plc_id))
    if machine_id.isdigit():
        edit_machine = get_object_or_404(
            Machine.objects.prefetch_related("signal_mappings"),
            pk=int(machine_id),
        )
    elif clone_id.isdigit():
        clone_source = get_object_or_404(
            Machine.objects.prefetch_related("signal_mappings"),
            pk=int(clone_id),
            controller__isnull=False,
        )

    plc_form = PlcControllerForm(instance=edit_plc, prefix="plc")
    machine_form = MachineProvisionForm(
        machine=edit_machine,
        copy_from=clone_source,
        prefix="machine",
    )

    if request.method == "POST":
        action = request.POST.get("action", "").strip()

        if action == "save_plc":
            target = None
            target_id = request.POST.get("target_id", "").strip()
            if target_id.isdigit():
                target = get_object_or_404(PlcController, pk=int(target_id))
            edit_plc = target
            plc_form = PlcControllerForm(request.POST, instance=target, prefix="plc")
            if plc_form.is_valid():
                plc = plc_form.save()
                messages.success(
                    request,
                    f"Đã lưu PLC {plc.code}. Collector sẽ tự nạp lại cấu hình.",
                )
                return redirect("monitoring:configuration")

        elif action == "save_machine":
            target = None
            target_id = request.POST.get("target_id", "").strip()
            if target_id.isdigit():
                target = get_object_or_404(
                    Machine.objects.prefetch_related("signal_mappings"),
                    pk=int(target_id),
                )

            post_clone_source = None
            clone_source_id = request.POST.get("clone_source_id", "").strip()
            if target is None and clone_source_id.isdigit():
                post_clone_source = get_object_or_404(
                    Machine.objects.prefetch_related("signal_mappings"),
                    pk=int(clone_source_id),
                    controller__isnull=False,
                )

            edit_machine = target
            clone_source = post_clone_source
            machine_form = MachineProvisionForm(
                request.POST,
                machine=target,
                copy_from=post_clone_source,
                prefix="machine",
            )
            if machine_form.is_valid():
                machine = machine_form.save()
                messages.success(
                    request,
                    f"Đã lưu {machine.code} và mapping. Collector sẽ tự đọc cấu hình mới.",
                )
                return redirect("monitoring:configuration")

    controllers = list(
        PlcController.objects
        .prefetch_related("machines")
        .order_by("code")
    )
    configured_machines = list(
        Machine.objects
        .filter(controller__isnull=False)
        .select_related("controller")
        .prefetch_related("signal_mappings")
        .order_by("code")
    )

    # Phát hiện cả dữ liệu trùng đã tồn tại từ trước khi bật strict guard.
    # Không tự xóa dữ liệu cũ để tránh mất lịch sử/cấu hình; UI sẽ cảnh báo
    # và buộc người dùng sửa mapping trước khi lưu/bật lại.
    conflicted_machine_count = 0
    for item in configured_machines:
        enabled_mappings = [m for m in item.signal_mappings.all() if m.is_enabled]
        specs = [
            (m.signal, m.address, m.data_type, m.word_order)
            for m in enabled_mappings
        ]
        item.mapping_conflicts = find_mapping_conflicts(
            item.controller,
            specs,
            exclude_machine=item,
        )
        if item.mapping_conflicts:
            conflicted_machine_count += 1

    return render(
        request,
        "monitoring/configuration.html",
        {
            "page_key": "configuration",
            "page_title": "Cấu hình giám sát",
            "page_eyebrow": "PLC · MACHINE · SIGNAL MAPPING",
            "machines": list(_active_machines()),
            "controllers": controllers,
            "configured_machines": configured_machines,
            "plc_form": plc_form,
            "machine_form": machine_form,
            "edit_plc": edit_plc,
            "edit_machine": edit_machine,
            "clone_source": clone_source,
            "conflicted_machine_count": conflicted_machine_count,
            "current_time": timezone.localtime(),
        },
    )


@require_POST
def toggle_machine(request, pk):
    machine = get_object_or_404(
        Machine.objects.prefetch_related("signal_mappings"),
        pk=pk,
        controller__isnull=False,
    )

    if not machine.is_active:
        mappings = [m for m in machine.signal_mappings.all() if m.is_enabled]
        specs = [
            (m.signal, m.address, m.data_type, m.word_order)
            for m in mappings
        ]
        conflicts = find_mapping_conflicts(
            machine.controller,
            specs,
            exclude_machine=machine,
        )
        if conflicts:
            messages.error(
                request,
                f"Không thể bật {machine.code}: mapping đang trùng Machine khác trên cùng PLC. "
                + "; ".join(conflicts[:4])
                + ("; ..." if len(conflicts) > 4 else ""),
            )
            return redirect("monitoring:configuration")

    machine.is_active = not machine.is_active
    machine.save(update_fields=["is_active", "updated_at"])
    messages.success(
        request,
        f"{machine.code}: {'đã bật giám sát' if machine.is_active else 'đã tạm dừng giám sát'}.",
    )
    return redirect("monitoring:configuration")


@require_POST
def toggle_controller(request, pk):
    controller = get_object_or_404(PlcController, pk=pk)
    controller.is_active = not controller.is_active
    controller.save(update_fields=["is_active", "updated_at"])
    messages.success(
        request,
        f"{controller.code}: {'đã bật' if controller.is_active else 'đã tạm dừng'} collector.",
    )
    return redirect("monitoring:configuration")

def health(request):
    """Health endpoint không lộ IP/credential; dùng cho giám sát dịch vụ."""
    now = timezone.now()
    controllers = list(PlcController.objects.filter(is_active=True))
    controller_states = []
    for controller in controllers:
        stale_limit = max(
            STALE_AFTER_SECONDS,
            int(controller.poll_interval_ms / 1000 * 3) + 1,
        )
        last_poll_age = (
            None
            if controller.last_poll_at is None
            else max(0, int((now - controller.last_poll_at).total_seconds()))
        )
        last_seen_age = (
            None
            if controller.last_seen_at is None
            else max(0, int((now - controller.last_seen_at).total_seconds()))
        )
        collector_fresh = last_poll_age is not None and last_poll_age <= stale_limit
        plc_fresh = last_seen_age is not None and last_seen_age <= stale_limit
        controller_states.append(
            {
                "code": controller.code,
                "collector_fresh": collector_fresh,
                "plc_online": plc_fresh,
                "last_poll_age_seconds": last_poll_age,
                "last_seen_age_seconds": last_seen_age,
            }
        )

    all_healthy = bool(controller_states) and all(
        item["collector_fresh"] and item["plc_online"]
        for item in controller_states
    )
    payload = {
        "status": "ok" if all_healthy else "degraded",
        "time": now.isoformat(),
        "active_controllers": len(controllers),
        "healthy_controllers": sum(
            item["collector_fresh"] and item["plc_online"]
            for item in controller_states
        ),
        "controllers": controller_states,
    }
    return JsonResponse(payload)


def legacy_live_data(request):
    code = request.GET.get("machine", "").strip()

    if not code:
        first = _active_machines().first()
        code = first.code if first else ""

    if code:
        return redirect(
            "monitoring:machine_detail",
            code=code,
        )

    return redirect("monitoring:dashboard")


def legacy_plc_signals(request):
    return legacy_live_data(request)
