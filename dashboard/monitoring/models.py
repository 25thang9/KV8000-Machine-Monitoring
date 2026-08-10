from django.db import models
from django.utils import timezone


class Machine(models.Model):
    """Thông tin cơ bản của máy được giám sát."""

    code = models.CharField(
        max_length=50,
        unique=True,
    )

    name = models.CharField(
        max_length=150,
    )

    description = models.TextField(
        blank=True,
    )

    is_active = models.BooleanField(
        default=True,
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
    )

    updated_at = models.DateTimeField(
        auto_now=True,
    )

    class Meta:
        db_table = "monitoring_machine"
        ordering = ["code"]

    def __str__(self):
        return f"{self.code} - {self.name}"


class MachineReading(models.Model):
    """Một lần thu thập dữ liệu từ máy."""

    class DataSource(models.TextChoices):
        MOCK = "MOCK", "Dữ liệu mô phỏng"
        PLC = "PLC", "KEYENCE KV-8000"

    machine = models.ForeignKey(
        Machine,
        on_delete=models.PROTECT,
        related_name="readings",
    )

    recorded_at = models.DateTimeField(
        default=timezone.now,
        db_index=True,
    )

    plc_online = models.BooleanField(
        default=False,
    )

    run_bit = models.BooleanField(
        default=False,
    )

    stop_bit = models.BooleanField(
        default=False,
    )

    alarm_bit = models.BooleanField(
        default=False,
    )

    auto_mode_bit = models.BooleanField(
        default=False,
    )

    production_count = models.BigIntegerField(
        default=0,
    )

    cycle_time_ms = models.IntegerField(
        null=True,
        blank=True,
    )

    alarm_code = models.IntegerField(
        default=0,
    )

    recipe_no = models.IntegerField(
        default=0,
    )

    source = models.CharField(
        max_length=10,
        choices=DataSource.choices,
        default=DataSource.MOCK,
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
    )

    class Meta:
        db_table = "monitoring_machine_reading"
        ordering = ["-recorded_at"]

        indexes = [
            models.Index(
                fields=["machine", "recorded_at"],
                name="mon_machine_time_idx",
            ),
        ]

    @property
    def status_label(self):
        """
        Trả về trạng thái dùng trên Dashboard.

        MOCK:
        Hiển thị RUN, STOP hoặc ALARM theo dữ liệu mô phỏng.

        PLC:
        Nếu mất kết nối thì hiển thị OFFLINE.
        """

        if (
            self.source == self.DataSource.PLC
            and not self.plc_online
        ):
            return "OFFLINE"

        if self.alarm_bit:
            return "ALARM"

        if self.run_bit:
            return "RUN"

        if self.stop_bit:
            return "STOP"

        return "UNKNOWN"

    def __str__(self):
        return (
            f"{self.machine.code} - "
            f"{self.recorded_at:%Y-%m-%d %H:%M:%S}"
        )