from django.core.management.base import BaseCommand, CommandError

from monitoring.models import Machine, PlcController, SignalMapping


DEFAULT_MAPPINGS = [
    (SignalMapping.Signal.RUN, "MR100", SignalMapping.DataType.BIT),
    (SignalMapping.Signal.STOP, "MR101", SignalMapping.DataType.BIT),
    (SignalMapping.Signal.ALARM, "MR102", SignalMapping.DataType.BIT),
    (SignalMapping.Signal.AUTO_MODE, "MR103", SignalMapping.DataType.BIT),
    (SignalMapping.Signal.PRODUCTION_COUNT, "DM1000", SignalMapping.DataType.UINT16),
    (SignalMapping.Signal.CYCLE_TIME_MS, "DM1002", SignalMapping.DataType.UINT16),
    (SignalMapping.Signal.ALARM_CODE, "DM1004", SignalMapping.DataType.UINT16),
    (SignalMapping.Signal.RECIPE_NO, "DM1005", SignalMapping.DataType.UINT16),
]


class Command(BaseCommand):
    help = "Tạo/cập nhật một PLC và một machine với mapping Host Link mặc định."

    def add_arguments(self, parser):
        parser.add_argument("--plc-code", required=True)
        parser.add_argument("--plc-name", default="KEYENCE PLC")
        parser.add_argument("--host", required=True)
        parser.add_argument("--port", type=int, default=8501)
        parser.add_argument("--machine-code", required=True)
        parser.add_argument("--machine-name", default="Production Machine")
        parser.add_argument("--poll-ms", type=int, default=1000)
        parser.add_argument("--history-seconds", type=int, default=30)
        parser.add_argument(
            "--overwrite-mappings",
            action="store_true",
            help="Ghi đè mapping 8 tín hiệu mặc định nếu mapping đã tồn tại.",
        )

    def handle(self, *args, **options):
        if not (1 <= options["port"] <= 65535):
            raise CommandError("Port phải nằm trong 1..65535")
        if options["poll_ms"] < 100:
            raise CommandError("poll-ms tối thiểu 100 ms")
        if options["history_seconds"] < 1:
            raise CommandError("history-seconds phải >= 1")

        plc, _created = PlcController.objects.update_or_create(
            code=options["plc_code"].strip(),
            defaults={
                "name": options["plc_name"].strip(),
                "host": options["host"].strip(),
                "port": options["port"],
                "poll_interval_ms": options["poll_ms"],
                "history_interval_seconds": options["history_seconds"],
                "is_active": True,
            },
        )

        machine, _created = Machine.objects.update_or_create(
            code=options["machine_code"].strip(),
            defaults={
                "name": options["machine_name"].strip(),
                "controller": plc,
                "is_active": True,
            },
        )

        created_count = 0
        updated_count = 0
        for signal, address, data_type in DEFAULT_MAPPINGS:
            defaults = {
                "address": address,
                "data_type": data_type,
                "word_order": SignalMapping.WordOrder.LOW_HIGH,
                "scale": 1,
                "offset": 0,
                "is_enabled": True,
            }
            if options["overwrite_mappings"]:
                _mapping, created = SignalMapping.objects.update_or_create(
                    machine=machine,
                    signal=signal,
                    defaults=defaults,
                )
                updated_count += 0 if created else 1
                created_count += 1 if created else 0
            else:
                _mapping, created = SignalMapping.objects.get_or_create(
                    machine=machine,
                    signal=signal,
                    defaults=defaults,
                )
                created_count += 1 if created else 0

        self.stdout.write(self.style.SUCCESS(
            f"READY: {plc.code} {plc.host}:{plc.port} -> {machine.code}"
        ))
        self.stdout.write(
            f"Mappings mới: {created_count}; cập nhật: {updated_count}."
        )
        self.stdout.write(
            "DM1000/DM1002 đang để UINT16. Nếu PLC thật dùng 2-word/DWORD, "
            "đổi Data type sang UINT32/INT32 trong Admin sau khi xác nhận KV STUDIO."
        )
