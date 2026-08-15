from django.core.management.base import BaseCommand, CommandError

from monitoring.models import Machine, MachineReading


class Command(BaseCommand):
    help = "Dọn dữ liệu MOCK cũ của phiên bản trước. Chỉ chạy khi đã backup DB."

    def add_arguments(self, parser):
        parser.add_argument("--confirm", action="store_true")

    def handle(self, *args, **options):
        mock_qs = MachineReading.objects.filter(source="MOCK")
        mock_count = mock_qs.count()
        orphan_machines = Machine.objects.filter(controller__isnull=True)
        self.stdout.write(
            f"MOCK readings: {mock_count}; machines chưa gán PLC: {orphan_machines.count()}"
        )
        if not options["confirm"]:
            raise CommandError(
                "Chưa xóa. Backup DB rồi chạy lại với --confirm nếu muốn dọn dữ liệu cũ."
            )
        deleted, _ = mock_qs.delete()
        orphan_machines.update(is_active=False)
        self.stdout.write(self.style.SUCCESS(
            f"Đã xóa {deleted} row liên quan MOCK và deactivate machine chưa gán PLC."
        ))
