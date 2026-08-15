from django.core.management.base import BaseCommand, CommandError

from collector.keyence_hostlink import KeyenceHostLinkClient
from monitoring.models import PlcController


class Command(BaseCommand):
    help = "Test read-only Host Link TCP cho PLC đã cấu hình, không ghi DB reading."

    def add_arguments(self, parser):
        parser.add_argument("--controller", help="PLC code; bỏ trống để test tất cả PLC active")

    def handle(self, *args, **options):
        qs = PlcController.objects.filter(is_active=True).prefetch_related(
            "machines__signal_mappings"
        ).order_by("code")
        if options.get("controller"):
            qs = qs.filter(code=options["controller"].strip())

        controllers = list(qs)
        if not controllers:
            raise CommandError("Không tìm thấy PLC active phù hợp.")

        failed_controllers = 0
        failed_machines = 0
        for plc in controllers:
            self.stdout.write(f"\n[{plc.code}] {plc.host}:{plc.port}")
            try:
                with KeyenceHostLinkClient(
                    plc.host,
                    plc.port,
                    connect_timeout=plc.connect_timeout_ms / 1000,
                    read_timeout=plc.read_timeout_ms / 1000,
                ) as client:
                    machine_count = 0
                    machine_success = 0
                    for machine in plc.machines.filter(is_active=True).order_by("code"):
                        mappings = list(machine.signal_mappings.filter(is_enabled=True))
                        if not mappings:
                            failed_machines += 1
                            self.stdout.write(self.style.WARNING(
                                f"  {machine.code}: chưa có mapping"
                            ))
                            continue
                        machine_count += 1
                        try:
                            values = client.read_mappings(mappings)
                        except Exception as exc:
                            failed_machines += 1
                            self.stdout.write(self.style.ERROR(
                                f"  {machine.code}: FAIL: {exc}"
                            ))
                            continue

                        machine_success += 1
                        rendered = ", ".join(
                            f"{key}={value}" for key, value in values.items()
                        )
                        self.stdout.write(self.style.SUCCESS(
                            f"  {machine.code}: {rendered}"
                        ))

                    if machine_count == 0:
                        self.stdout.write(self.style.WARNING(
                            "  Không có machine mapping để đọc."
                        ))
                    elif machine_success == 0:
                        failed_controllers += 1
            except Exception as exc:
                failed_controllers += 1
                self.stdout.write(self.style.ERROR(f"  PLC CONNECT FAIL: {exc}"))

        if failed_controllers or failed_machines:
            raise CommandError(
                f"Kiểm tra thất bại: {failed_controllers} PLC; "
                f"{failed_machines} machine/mapping."
            )
