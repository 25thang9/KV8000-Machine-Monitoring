from __future__ import annotations

import re

from django import forms
from django.core.exceptions import ValidationError
from django.db import transaction

from .models import Machine, PlcController, SignalMapping


NUMERIC_TYPES = (
    (SignalMapping.DataType.UINT16, "UINT16 · 1 word"),
    (SignalMapping.DataType.INT16, "INT16 · 1 word"),
    (SignalMapping.DataType.UINT32, "UINT32 · 2 words"),
    (SignalMapping.DataType.INT32, "INT32 · 2 words"),
)

WORD_ORDER_CHOICES = (
    (SignalMapping.WordOrder.LOW_HIGH, "Low word trước"),
    (SignalMapping.WordOrder.HIGH_LOW, "High word trước"),
)

def _mapping_span(address: str, data_type: str):
    """Return a comparable device span for collision detection.

    Exact address collisions are always checked. Numeric word ranges are also
    compared for 16/32-bit values so DM1000 UINT32 collides with DM1001.
    """
    normalized = (address or "").strip().upper()
    match = re.fullmatch(r"([A-Z]{1,3})(\d+)", normalized)
    if not match:
        return normalized, None
    prefix, number_text = match.groups()
    start = int(number_text)
    width = 2 if data_type in {
        SignalMapping.DataType.UINT32,
        SignalMapping.DataType.INT32,
    } else 1
    return normalized, (prefix, start, start + width - 1)


def find_mapping_conflicts(controller, specs, *, exclude_machine=None):
    """Find address collisions with ANY other machine on the same PLC.

    A device/range may belong to only one Machine within one PLC, regardless of
    whether either Machine is ACTIVE or PAUSED. This keeps clone/import/edit
    workflows unambiguous and prevents a paused duplicate from becoming a
    future production error. The same address on a different PLC is valid.
    """
    if controller is None:
        return []

    qs = (
        SignalMapping.objects
        .select_related("machine")
        .filter(
            machine__controller=controller,
            is_enabled=True,
        )
    )
    if exclude_machine is not None and exclude_machine.pk:
        qs = qs.exclude(machine=exclude_machine)

    existing = []
    for mapping in qs:
        normalized, span = _mapping_span(mapping.address, mapping.data_type)
        existing.append((mapping, normalized, span))

    conflicts = []
    seen = set()
    for signal, address, data_type, _word_order in specs:
        normalized = (address or "").strip().upper()
        _normalized, span = _mapping_span(normalized, data_type)
        for mapping, existing_address, existing_span in existing:
            collision = normalized == existing_address
            if not collision and span is not None and existing_span is not None:
                prefix, start, end = span
                e_prefix, e_start, e_end = existing_span
                collision = (
                    prefix == e_prefix
                    and start <= e_end
                    and e_start <= end
                )
            if collision:
                key = (signal, normalized, mapping.machine_id, mapping.signal)
                if key in seen:
                    continue
                seen.add(key)
                conflicts.append(
                    f"{signal}={normalized} trùng/chồng với "
                    f"{mapping.machine.code} ({mapping.signal}={mapping.address})"
                )
    return conflicts



class PlcControllerForm(forms.ModelForm):
    class Meta:
        model = PlcController
        fields = (
            "code",
            "name",
            "host",
            "port",
            "poll_interval_ms",
            "connect_timeout_ms",
            "read_timeout_ms",
            "history_interval_seconds",
            "offline_write_interval_seconds",
            "is_active",
        )
        widgets = {
            "code": forms.TextInput(attrs={"placeholder": "PLC01"}),
            "name": forms.TextInput(attrs={"placeholder": "KEYENCE KV-7500"}),
            "host": forms.TextInput(attrs={"placeholder": "192.168.0.10"}),
        }

    def clean_code(self):
        return (self.cleaned_data["code"] or "").strip().upper()

    def clean_host(self):
        return (self.cleaned_data["host"] or "").strip()


class MachineProvisionForm(forms.Form):
    """Tạo/cập nhật một Machine cùng đủ 8 mapping trong một form.

    Form này chỉ ghi cấu hình database. Nó KHÔNG SET/FORCE/WRITE PLC.
    """

    machine_code = forms.CharField(max_length=50, label="Mã máy")
    machine_name = forms.CharField(max_length=150, label="Tên máy")
    description = forms.CharField(
        required=False,
        label="Mô tả",
        widget=forms.Textarea(attrs={"rows": 2, "class": "machine-description"}),
    )
    controller = forms.ModelChoiceField(
        queryset=PlcController.objects.none(),
        label="PLC",
    )
    is_active = forms.BooleanField(required=False, initial=True, label="Đang giám sát")

    run_address = forms.CharField(label="RUN", widget=forms.TextInput(attrs={"placeholder": "ví dụ MR100", "class": "mapping-device-input"}))
    stop_address = forms.CharField(label="STOP", widget=forms.TextInput(attrs={"placeholder": "ví dụ MR101", "class": "mapping-device-input"}))
    alarm_address = forms.CharField(label="ALARM", widget=forms.TextInput(attrs={"placeholder": "ví dụ MR102", "class": "mapping-device-input"}))
    auto_address = forms.CharField(label="AUTO", widget=forms.TextInput(attrs={"placeholder": "ví dụ MR103", "class": "mapping-device-input"}))

    production_address = forms.CharField(label="Production Count", widget=forms.TextInput(attrs={"placeholder": "ví dụ DM1000", "class": "mapping-device-input"}))
    production_type = forms.ChoiceField(label="Kiểu Count", choices=NUMERIC_TYPES)
    cycle_address = forms.CharField(label="Cycle Time", widget=forms.TextInput(attrs={"placeholder": "ví dụ DM1002", "class": "mapping-device-input"}))
    cycle_type = forms.ChoiceField(label="Kiểu Cycle", choices=NUMERIC_TYPES)
    alarm_code_address = forms.CharField(label="Alarm Code", widget=forms.TextInput(attrs={"placeholder": "ví dụ DM1004", "class": "mapping-device-input"}))
    alarm_code_type = forms.ChoiceField(label="Kiểu Alarm Code", choices=NUMERIC_TYPES)
    recipe_address = forms.CharField(label="Recipe", widget=forms.TextInput(attrs={"placeholder": "ví dụ DM1005", "class": "mapping-device-input"}))
    recipe_type = forms.ChoiceField(label="Kiểu Recipe", choices=NUMERIC_TYPES)
    production_word_order = forms.ChoiceField(
        label="Word order Count",
        choices=WORD_ORDER_CHOICES,
        initial=SignalMapping.WordOrder.LOW_HIGH,
    )
    cycle_word_order = forms.ChoiceField(
        label="Word order Cycle",
        choices=WORD_ORDER_CHOICES,
        initial=SignalMapping.WordOrder.LOW_HIGH,
    )
    alarm_code_word_order = forms.ChoiceField(
        label="Word order Alarm Code",
        choices=WORD_ORDER_CHOICES,
        initial=SignalMapping.WordOrder.LOW_HIGH,
    )
    recipe_word_order = forms.ChoiceField(
        label="Word order Recipe",
        choices=WORD_ORDER_CHOICES,
        initial=SignalMapping.WordOrder.LOW_HIGH,
    )

    def __init__(
        self,
        *args,
        machine: Machine | None = None,
        copy_from: Machine | None = None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.machine = machine
        self.copy_from = copy_from if machine is None else None
        self._clone_calibration = {}
        self.fields["controller"].queryset = PlcController.objects.order_by("code")

        source = machine or self.copy_from
        mappings = {}
        if source is not None:
            mappings = {m.signal: m for m in source.signal_mappings.all()}
            if self.copy_from is not None:
                # Clone phải giữ nguyên calibration scale/offset của máy nguồn
                # kể cả ở lần POST sau khi người dùng đã sửa Code/MR/DM.
                self._clone_calibration = {
                    signal: (mapping.scale, mapping.offset)
                    for signal, mapping in mappings.items()
                }

        if source is not None and not self.is_bound:
            if machine is not None:
                machine_code = machine.code
                machine_name = machine.name
                is_active = machine.is_active
            else:
                # Clone chỉ sao chép cấu hình làm mẫu. Không tự bật giám sát vì
                # các device vẫn đang trùng với máy nguồn cho đến khi người dùng
                # xác nhận/sửa mapping theo PLC thật.
                machine_code = ""
                machine_name = f"{source.name} - Copy"
                is_active = False

            self.initial.update(
                {
                    "machine_code": machine_code,
                    "machine_name": machine_name,
                    "description": source.description,
                    "controller": source.controller_id,
                    "is_active": is_active,
                }
            )
            field_map = {
                SignalMapping.Signal.RUN: ("run_address", None, None),
                SignalMapping.Signal.STOP: ("stop_address", None, None),
                SignalMapping.Signal.ALARM: ("alarm_address", None, None),
                SignalMapping.Signal.AUTO_MODE: ("auto_address", None, None),
                SignalMapping.Signal.PRODUCTION_COUNT: (
                    "production_address", "production_type", "production_word_order"
                ),
                SignalMapping.Signal.CYCLE_TIME_MS: (
                    "cycle_address", "cycle_type", "cycle_word_order"
                ),
                SignalMapping.Signal.ALARM_CODE: (
                    "alarm_code_address", "alarm_code_type", "alarm_code_word_order"
                ),
                SignalMapping.Signal.RECIPE_NO: (
                    "recipe_address", "recipe_type", "recipe_word_order"
                ),
            }
            for signal, (address_field, type_field, order_field) in field_map.items():
                mapping = mappings.get(signal)
                if not mapping:
                    continue
                self.initial[address_field] = mapping.address
                if type_field:
                    self.initial[type_field] = mapping.data_type
                if order_field:
                    self.initial[order_field] = mapping.word_order

    def clean_machine_code(self):
        code = (self.cleaned_data["machine_code"] or "").strip().upper()
        existing = Machine.objects.filter(code=code).first()
        if existing and (self.machine is None or existing.pk != self.machine.pk):
            raise ValidationError("Mã máy này đã tồn tại.")
        return code

    def clean(self):
        cleaned = super().clean()
        if self.errors:
            return cleaned

        specs = self._mapping_specs(cleaned)
        normalized_addresses = {}
        numeric_ranges = []
        for signal, address, data_type, word_order in specs:
            mapping = SignalMapping(
                machine=self.machine or Machine(code="TEMP", name="TEMP"),
                signal=signal,
                address=address,
                data_type=data_type,
                word_order=word_order,
                scale=1,
                offset=0,
                is_enabled=True,
            )
            try:
                # clean() không cần machine đã save để kiểm tra address/type.
                mapping.clean()
            except ValidationError as exc:
                self.add_error(None, f"{signal}: {exc}")
                continue

            normalized = mapping.address
            previous = normalized_addresses.get(normalized)
            if previous is not None:
                self.add_error(
                    None,
                    f"{signal}: device {normalized} đang trùng với {previous}.",
                )
            else:
                normalized_addresses[normalized] = signal

            # Với giá trị 32-bit, base device chiếm 2 word. Bắt lỗi vùng DM
            # chồng lấn (ví dụ Count=DM1000 UINT32 và Cycle=DM1001) ngay lúc
            # cấu hình thay vì để Collector đọc ra số hợp lệ nhưng sai nghĩa.
            if data_type != SignalMapping.DataType.BIT:
                match = re.fullmatch(r"([A-Z]{1,3})(\d+)", normalized)
                if match:
                    prefix, number_text = match.groups()
                    start = int(number_text)
                    width = 2 if data_type in {
                        SignalMapping.DataType.UINT32,
                        SignalMapping.DataType.INT32,
                    } else 1
                    end = start + width - 1
                    for prev_prefix, prev_start, prev_end, prev_signal in numeric_ranges:
                        if (
                            prefix == prev_prefix
                            and start <= prev_end
                            and prev_start <= end
                        ):
                            self.add_error(
                                None,
                                f"{signal}: vùng {normalized}"
                                f"{'..' + prefix + str(end) if end != start else ''} "
                                f"chồng lấn với {prev_signal}.",
                            )
                            break
                    numeric_ranges.append((prefix, start, end, signal))

        # STRICT GUARD: trên cùng một PLC, mapping phải duy nhất ngay lúc Save,
        # kể cả Machine đang PAUSED. Clone chỉ copy làm mẫu; người dùng phải
        # sửa các MR/DM trùng trước khi có thể lưu Machine mới.
        if cleaned.get("controller"):
            conflicts = find_mapping_conflicts(
                cleaned["controller"],
                specs,
                exclude_machine=self.machine,
            )
            if conflicts:
                self.add_error(
                    None,
                    "Mapping đã được Machine khác sử dụng trên cùng PLC: "
                    + "; ".join(conflicts[:4])
                    + ("; ..." if len(conflicts) > 4 else ""),
                )
        return cleaned

    @staticmethod
    def _mapping_specs(cleaned):
        low_high = SignalMapping.WordOrder.LOW_HIGH
        return [
            (
                SignalMapping.Signal.RUN,
                cleaned.get("run_address", ""),
                SignalMapping.DataType.BIT,
                low_high,
            ),
            (
                SignalMapping.Signal.STOP,
                cleaned.get("stop_address", ""),
                SignalMapping.DataType.BIT,
                low_high,
            ),
            (
                SignalMapping.Signal.ALARM,
                cleaned.get("alarm_address", ""),
                SignalMapping.DataType.BIT,
                low_high,
            ),
            (
                SignalMapping.Signal.AUTO_MODE,
                cleaned.get("auto_address", ""),
                SignalMapping.DataType.BIT,
                low_high,
            ),
            (
                SignalMapping.Signal.PRODUCTION_COUNT,
                cleaned.get("production_address", ""),
                cleaned.get("production_type"),
                cleaned.get("production_word_order", low_high),
            ),
            (
                SignalMapping.Signal.CYCLE_TIME_MS,
                cleaned.get("cycle_address", ""),
                cleaned.get("cycle_type"),
                cleaned.get("cycle_word_order", low_high),
            ),
            (
                SignalMapping.Signal.ALARM_CODE,
                cleaned.get("alarm_code_address", ""),
                cleaned.get("alarm_code_type"),
                cleaned.get("alarm_code_word_order", low_high),
            ),
            (
                SignalMapping.Signal.RECIPE_NO,
                cleaned.get("recipe_address", ""),
                cleaned.get("recipe_type"),
                cleaned.get("recipe_word_order", low_high),
            ),
        ]

    @transaction.atomic
    def save(self) -> Machine:
        if not self.is_valid():
            raise ValueError("Không thể save MachineProvisionForm chưa hợp lệ.")

        data = self.cleaned_data

        # Re-check at save time as well. This also protects CSV all-or-nothing
        # imports where several forms were validated before the first row was
        # inserted into the database.
        conflicts = find_mapping_conflicts(
            data.get("controller"),
            self._mapping_specs(data),
            exclude_machine=self.machine,
        )
        if conflicts:
            raise ValueError(
                "Mapping đã được Machine khác sử dụng trên cùng PLC: "
                + "; ".join(conflicts[:4])
                + ("; ..." if len(conflicts) > 4 else "")
            )

        machine = self.machine or Machine()
        machine.code = data["machine_code"]
        machine.name = data["machine_name"].strip()
        machine.description = data["description"].strip()
        machine.controller = data["controller"]
        machine.is_active = data["is_active"]
        machine.full_clean()
        machine.save()

        for signal, address, data_type, word_order in self._mapping_specs(data):
            mapping, created = SignalMapping.objects.get_or_create(
                machine=machine,
                signal=signal,
                defaults={
                    "address": address,
                    "data_type": data_type,
                    "word_order": word_order,
                    "scale": 1,
                    "offset": 0,
                    "is_enabled": True,
                },
            )
            mapping.address = address
            mapping.data_type = data_type
            mapping.word_order = word_order

            # Nếu mapping đã được hiệu chỉnh scale/offset trong Admin, sửa
            # Machine ở trang cấu hình không được âm thầm reset calibration.
            if created:
                if signal in self._clone_calibration:
                    mapping.scale, mapping.offset = self._clone_calibration[signal]
                else:
                    mapping.scale = 1
                    mapping.offset = 0
            mapping.is_enabled = True
            mapping.full_clean()
            mapping.save()

        return machine
