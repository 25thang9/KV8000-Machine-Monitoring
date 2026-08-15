import re

from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone


DEVICE_ADDRESS_RE = re.compile(r"^([A-Z]{1,3})(\d+)$")


def validate_keyence_device_address(address: str) -> str:
    """Chuẩn hóa và kiểm tra lỗi nhập device phổ biến của KV Series.

    R/MR/CR dùng dạng channel + contact; contact hợp lệ là 00..15.
    Ví dụ MR9000, MR9015 hợp lệ nhưng MR9020 không hợp lệ.
    Các prefix khác vẫn được giữ mở để không khóa cứng mapping máy thật.
    """
    normalized = (address or "").strip().upper()
    match = DEVICE_ADDRESS_RE.fullmatch(normalized)
    if not match:
        raise ValidationError(
            "Device không hợp lệ. Ví dụ hợp lệ: MR100, DM1000."
        )

    prefix, digits = match.groups()
    if prefix in {"R", "MR", "CR"} and len(digits) >= 2:
        contact = int(digits[-2:])
        if contact > 15:
            raise ValidationError(
                f"{normalized}: contact phải nằm trong 00..15."
            )

        # Manual KV-7000 xác định MR theo channel 000..3999 + contact 00..15.
        # Chỉ khóa range cho MR vì R/CR có range riêng theo loại device.
        if prefix == "MR":
            channel = int(digits[:-2] or "0")
            if channel > 3999:
                raise ValidationError(
                    f"{normalized}: channel MR phải nằm trong 0000..3999."
                )
    return normalized


class PlcController(models.Model):
    """PLC/CPU vật lý được collector giám sát qua Host Link TCP."""

    class Protocol(models.TextChoices):
        KEYENCE_HOSTLINK_TCP = "KV_HOSTLINK_TCP", "KEYENCE Host Link TCP"

    code = models.CharField(max_length=50, unique=True)
    name = models.CharField(max_length=150)
    host = models.CharField(
        max_length=255,
        help_text="IPv4/hostname của PLC, ví dụ 192.168.0.10",
    )
    port = models.PositiveIntegerField(
        default=8501,
        validators=[MinValueValidator(1), MaxValueValidator(65535)],
    )
    protocol = models.CharField(
        max_length=32,
        choices=Protocol.choices,
        default=Protocol.KEYENCE_HOSTLINK_TCP,
    )
    poll_interval_ms = models.PositiveIntegerField(
        default=1000,
        validators=[MinValueValidator(100), MaxValueValidator(60000)],
        help_text="Chu kỳ đọc PLC. Khuyến nghị 500-5000 ms.",
    )
    connect_timeout_ms = models.PositiveIntegerField(
        default=2000,
        validators=[MinValueValidator(100), MaxValueValidator(60000)],
    )
    read_timeout_ms = models.PositiveIntegerField(
        default=2000,
        validators=[MinValueValidator(100), MaxValueValidator(60000)],
    )
    history_interval_seconds = models.PositiveIntegerField(
        default=30,
        validators=[MinValueValidator(1), MaxValueValidator(86400)],
        help_text=(
            "Dù collector poll nhanh hơn, một snapshot không đổi chỉ được lưu "
            "theo chu kỳ này để tránh phình database. Trạng thái/alarm/recipe "
            "được lưu lịch sử ngay; giá trị số realtime nằm ở CurrentState."
        ),
    )
    offline_write_interval_seconds = models.PositiveIntegerField(
        default=30,
        validators=[MinValueValidator(1), MaxValueValidator(86400)],
        help_text="Chu kỳ ghi heartbeat OFFLINE khi PLC mất kết nối.",
    )
    is_active = models.BooleanField(default=True)

    last_poll_at = models.DateTimeField(null=True, blank=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)
    consecutive_failures = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "monitoring_plc_controller"
        ordering = ["code"]

    def __str__(self):
        return f"{self.code} - {self.name} ({self.host}:{self.port})"

    @property
    def endpoint(self):
        return f"{self.host}:{self.port}"

    @property
    def health_label(self):
        if not self.is_active:
            return "DISABLED"
        if self.last_seen_at is None:
            return "CHƯA KẾT NỐI"
        stale_seconds = max(15, int(self.poll_interval_ms / 1000 * 3) + 1)
        age = (timezone.now() - self.last_seen_at).total_seconds()
        return "ONLINE" if age <= stale_seconds else "OFFLINE"


class Machine(models.Model):
    """Máy sản xuất được giám sát; mỗi máy thuộc một PLC vật lý."""

    code = models.CharField(max_length=50, unique=True)
    name = models.CharField(max_length=150)
    description = models.TextField(blank=True)
    controller = models.ForeignKey(
        PlcController,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="machines",
        help_text="PLC vật lý đang chứa các device của máy này.",
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "monitoring_machine"
        ordering = ["code"]

    def __str__(self):
        return f"{self.code} - {self.name}"


class SignalMapping(models.Model):
    """Mapping giữa field nghiệp vụ của web và device thật trên PLC."""

    class Signal(models.TextChoices):
        RUN = "RUN", "Máy chạy"
        STOP = "STOP", "Máy dừng"
        ALARM = "ALARM", "Cảnh báo"
        AUTO_MODE = "AUTO_MODE", "Chế độ tự động"
        PRODUCTION_COUNT = "PRODUCTION_COUNT", "Sản lượng"
        CYCLE_TIME_MS = "CYCLE_TIME_MS", "Thời gian chu kỳ"
        ALARM_CODE = "ALARM_CODE", "Mã cảnh báo"
        RECIPE_NO = "RECIPE_NO", "Recipe / Product No."

    class DataType(models.TextChoices):
        BIT = "BIT", "BIT"
        UINT16 = "UINT16", "UINT16 (1 word unsigned)"
        INT16 = "INT16", "INT16 (1 word signed)"
        UINT32 = "UINT32", "UINT32 (2 words unsigned)"
        INT32 = "INT32", "INT32 (2 words signed)"

    class WordOrder(models.TextChoices):
        LOW_HIGH = "LOW_HIGH", "Low word trước (DMn = low, DMn+1 = high)"
        HIGH_LOW = "HIGH_LOW", "High word trước (DMn = high, DMn+1 = low)"

    FIELD_BY_SIGNAL = {
        Signal.RUN: "run_bit",
        Signal.STOP: "stop_bit",
        Signal.ALARM: "alarm_bit",
        Signal.AUTO_MODE: "auto_mode_bit",
        Signal.PRODUCTION_COUNT: "production_count",
        Signal.CYCLE_TIME_MS: "cycle_time_ms",
        Signal.ALARM_CODE: "alarm_code",
        Signal.RECIPE_NO: "recipe_no",
    }

    UNIT_BY_SIGNAL = {
        Signal.RUN: "ON/OFF",
        Signal.STOP: "ON/OFF",
        Signal.ALARM: "ON/OFF",
        Signal.AUTO_MODE: "ON/OFF",
        Signal.PRODUCTION_COUNT: "sản phẩm",
        Signal.CYCLE_TIME_MS: "ms",
        Signal.ALARM_CODE: "mã",
        Signal.RECIPE_NO: "mã",
    }

    BIT_SIGNALS = {
        Signal.RUN,
        Signal.STOP,
        Signal.ALARM,
        Signal.AUTO_MODE,
    }

    machine = models.ForeignKey(
        Machine,
        on_delete=models.CASCADE,
        related_name="signal_mappings",
    )
    signal = models.CharField(max_length=32, choices=Signal.choices)
    address = models.CharField(
        max_length=32,
        help_text="Global device, ví dụ MR100 hoặc DM1000.",
    )
    data_type = models.CharField(
        max_length=16,
        choices=DataType.choices,
        default=DataType.UINT16,
    )
    word_order = models.CharField(
        max_length=16,
        choices=WordOrder.choices,
        default=WordOrder.LOW_HIGH,
        help_text="Chỉ dùng cho UINT32/INT32.",
    )
    scale = models.DecimalField(
        max_digits=12,
        decimal_places=6,
        default=1,
        help_text="Giá trị web = raw × scale + offset.",
    )
    offset = models.DecimalField(
        max_digits=12,
        decimal_places=6,
        default=0,
    )
    is_enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "monitoring_signal_mapping"
        ordering = ["machine__code", "signal"]
        constraints = [
            models.UniqueConstraint(
                fields=["machine", "signal"],
                name="uniq_machine_signal_mapping",
            )
        ]
        indexes = [
            models.Index(
                fields=["machine", "is_enabled"],
                name="mon_map_machine_enabled_idx",
            )
        ]

    def clean(self):
        super().clean()
        try:
            self.address = validate_keyence_device_address(self.address)
        except ValidationError as exc:
            raise ValidationError({"address": exc.messages}) from exc

        signal = self.signal
        is_bit_signal = signal in self.BIT_SIGNALS
        if is_bit_signal and self.data_type != self.DataType.BIT:
            raise ValidationError(
                {"data_type": "RUN/STOP/ALARM/AUTO_MODE phải dùng kiểu BIT."}
            )
        if not is_bit_signal and self.data_type == self.DataType.BIT:
            raise ValidationError(
                {"data_type": "Các giá trị số không được cấu hình kiểu BIT."}
            )

    @property
    def field_name(self):
        return self.FIELD_BY_SIGNAL[self.signal]

    @property
    def unit(self):
        return self.UNIT_BY_SIGNAL[self.signal]

    def __str__(self):
        return f"{self.machine.code} | {self.get_signal_display()} = {self.address}"


class MachineCurrentState(models.Model):
    """Một row trạng thái hiện tại/máy, được collector cập nhật mỗi poll.

    Bảng này tách dữ liệu realtime khỏi bảng lịch sử để dashboard vẫn cập nhật
    nhanh khi có nhiều máy mà không phải ghi một MachineReading mỗi giây.
    """

    machine = models.OneToOneField(
        Machine,
        primary_key=True,
        on_delete=models.CASCADE,
        related_name="current_state",
    )
    recorded_at = models.DateTimeField(default=timezone.now, db_index=True)
    plc_online = models.BooleanField(default=False)
    run_bit = models.BooleanField(default=False)
    stop_bit = models.BooleanField(default=False)
    alarm_bit = models.BooleanField(default=False)
    auto_mode_bit = models.BooleanField(default=False)
    production_count = models.BigIntegerField(default=0)
    cycle_time_ms = models.IntegerField(null=True, blank=True)
    alarm_code = models.IntegerField(default=0)
    recipe_no = models.IntegerField(default=0)
    source = models.CharField(max_length=10, default="PLC", editable=False)
    last_error = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "monitoring_machine_current_state"
        ordering = ["machine__code"]

    @property
    def status_label(self):
        if not self.plc_online:
            return "OFFLINE"
        if self.alarm_bit:
            return "ALARM"
        if self.run_bit and self.stop_bit:
            return "ABNORMAL"
        if self.run_bit:
            return "RUN"
        if self.stop_bit:
            return "STOP"
        return "UNKNOWN"

    def __str__(self):
        return f"{self.machine.code} - CURRENT"


class MachineReading(models.Model):
    """Snapshot lịch sử được collector thật thu từ PLC."""

    class DataSource(models.TextChoices):
        PLC = "PLC", "KEYENCE PLC"

    machine = models.ForeignKey(
        Machine,
        on_delete=models.PROTECT,
        related_name="readings",
    )
    recorded_at = models.DateTimeField(default=timezone.now, db_index=True)
    plc_online = models.BooleanField(default=False)
    run_bit = models.BooleanField(default=False)
    stop_bit = models.BooleanField(default=False)
    alarm_bit = models.BooleanField(default=False)
    auto_mode_bit = models.BooleanField(default=False)
    production_count = models.BigIntegerField(default=0)
    cycle_time_ms = models.IntegerField(null=True, blank=True)
    alarm_code = models.IntegerField(default=0)
    recipe_no = models.IntegerField(default=0)
    source = models.CharField(
        max_length=10,
        choices=DataSource.choices,
        default=DataSource.PLC,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "monitoring_machine_reading"
        ordering = ["-recorded_at"]
        indexes = [
            models.Index(
                fields=["machine", "recorded_at"],
                name="mon_machine_time_idx",
            ),
            models.Index(
                fields=["machine", "alarm_bit", "recorded_at"],
                name="mon_machine_alarm_time_idx",
            ),
        ]

    @property
    def status_label(self):
        if not self.plc_online:
            return "OFFLINE"
        if self.alarm_bit:
            return "ALARM"
        if self.run_bit and self.stop_bit:
            return "ABNORMAL"
        if self.run_bit:
            return "RUN"
        if self.stop_bit:
            return "STOP"
        return "UNKNOWN"

    def __str__(self):
        return f"{self.machine.code} - {self.recorded_at:%Y-%m-%d %H:%M:%S}"
