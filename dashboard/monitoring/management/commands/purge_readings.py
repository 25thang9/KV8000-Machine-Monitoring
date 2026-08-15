from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from monitoring.models import MachineReading


class Command(BaseCommand):
    help = "Xóa lịch sử cũ theo retention, theo batch để giảm lock database."

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=settings.COLLECTOR_RETENTION_DAYS,
        )
        parser.add_argument("--batch-size", type=int, default=10000)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        days = max(1, options["days"])
        batch_size = max(100, options["batch_size"])
        cutoff = timezone.now() - timedelta(days=days)
        qs = MachineReading.objects.filter(recorded_at__lt=cutoff)
        total = qs.count()
        self.stdout.write(
            f"Cutoff: {cutoff.isoformat()} | rows cần xóa: {total}"
        )
        if options["dry_run"]:
            return

        deleted_total = 0
        while True:
            ids = list(qs.order_by("pk").values_list("pk", flat=True)[:batch_size])
            if not ids:
                break
            deleted, _details = MachineReading.objects.filter(pk__in=ids).delete()
            deleted_total += deleted
            self.stdout.write(f"Đã xóa: {deleted_total}")

        self.stdout.write(self.style.SUCCESS(f"DONE: {deleted_total} rows"))
