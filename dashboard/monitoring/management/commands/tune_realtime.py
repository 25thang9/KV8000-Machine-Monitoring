from django.core.management.base import BaseCommand, CommandError

from monitoring.models import PlcController


class Command(BaseCommand):
    help = "Đặt chu kỳ poll realtime cho PLC active mà không đổi schema/database mapping."

    def add_arguments(self, parser):
        parser.add_argument(
            "--poll-ms",
            type=int,
            default=250,
            help="Chu kỳ đọc PLC theo millisecond (100..60000), mặc định 250 ms.",
        )
        parser.add_argument(
            "--controller",
            default="",
            help="Chỉ áp dụng cho PlcController code này. Bỏ trống = mọi PLC active.",
        )

    def handle(self, *args, **options):
        poll_ms = int(options["poll_ms"])
        if not 100 <= poll_ms <= 60000:
            raise CommandError("--poll-ms phải nằm trong 100..60000.")

        queryset = PlcController.objects.filter(is_active=True)
        code = (options.get("controller") or "").strip()
        if code:
            queryset = queryset.filter(code=code)

        controllers = list(queryset.order_by("code"))
        if not controllers:
            raise CommandError("Không tìm thấy PlcController active phù hợp.")

        for controller in controllers:
            old = controller.poll_interval_ms
            controller.poll_interval_ms = poll_ms
            controller.save(update_fields=["poll_interval_ms", "updated_at"])
            self.stdout.write(
                self.style.SUCCESS(
                    f"{controller.code}: {old} ms -> {poll_ms} ms"
                )
            )

        self.stdout.write(
            self.style.SUCCESS(
                "DONE. Collector sẽ nhận cấu hình mới sau lần refresh config kế tiếp."
            )
        )
