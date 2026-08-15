from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from collector.keyence_hostlink import DeviceAddress
from monitoring.models import Machine, PlcController, SignalMapping


class Command(BaseCommand):
    help = "Validate cấu hình PLC/machine/mapping trong database, không kết nối PLC."

    def add_arguments(self, parser):
        parser.add_argument(
            "--strict",
            action="store_true",
            help="Thiếu bất kỳ signal chuẩn nào cũng được coi là lỗi.",
        )

    def handle(self, *args, **options):
        errors = []
        warnings = []
        expected_signals = {value for value, _label in SignalMapping.Signal.choices}

        controllers = list(PlcController.objects.filter(is_active=True).order_by("code"))
        if not controllers:
            errors.append("Không có PlcController active.")

        endpoint_owner = {}
        for plc in controllers:
            try:
                plc.full_clean()
            except ValidationError as exc:
                errors.append(f"{plc.code}: {exc}")
            endpoint = (plc.host.strip().lower(), plc.port)
            previous = endpoint_owner.get(endpoint)
            if previous:
                errors.append(
                    f"{plc.code} và {previous} trùng endpoint {plc.host}:{plc.port}. "
                    "Một PLC vật lý chỉ nên có một PlcController; gán nhiều Machine vào controller đó."
                )
            else:
                endpoint_owner[endpoint] = plc.code

        machines = list(
            Machine.objects.filter(is_active=True)
            .select_related("controller")
            .prefetch_related("signal_mappings")
            .order_by("code")
        )
        if not machines:
            errors.append("Không có Machine active.")

        for machine in machines:
            if machine.controller_id is None:
                errors.append(f"{machine.code}: chưa gán PLC controller.")
                continue
            if not machine.controller.is_active:
                errors.append(f"{machine.code}: controller {machine.controller.code} đang disabled.")

            mappings = [item for item in machine.signal_mappings.all() if item.is_enabled]
            if not mappings:
                errors.append(f"{machine.code}: chưa có SignalMapping active.")
                continue

            mapped_signals = set()
            occupied = {}
            for mapping in mappings:
                try:
                    mapping.full_clean()
                except ValidationError as exc:
                    errors.append(f"{machine.code}/{mapping.signal}: {exc}")
                    continue
                mapped_signals.add(mapping.signal)

                address = DeviceAddress.parse(mapping.address)
                width = 2 if mapping.data_type in {
                    SignalMapping.DataType.UINT32,
                    SignalMapping.DataType.INT32,
                } else 1
                for number in range(address.number, address.number + width):
                    slot = (address.prefix, number)
                    previous = occupied.get(slot)
                    if previous is not None:
                        message = (
                            f"{machine.code}: mapping {mapping.signal} ({mapping.address}) "
                            f"chồng device với {previous}."
                        )
                        if options["strict"]:
                            errors.append(message)
                        else:
                            warnings.append(message)
                    else:
                        occupied[slot] = f"{mapping.signal} ({mapping.address})"

            missing = expected_signals - mapped_signals
            if missing:
                message = f"{machine.code}: thiếu signal {', '.join(sorted(missing))}."
                if options["strict"]:
                    errors.append(message)
                else:
                    warnings.append(message)

        for warning in warnings:
            self.stdout.write(self.style.WARNING(f"WARN: {warning}"))
        for error in errors:
            self.stdout.write(self.style.ERROR(f"ERROR: {error}"))

        if errors:
            raise CommandError(
                f"Cấu hình chưa đạt: {len(errors)} lỗi, {len(warnings)} cảnh báo."
            )

        self.stdout.write(self.style.SUCCESS(
            f"CONFIG OK: {len(controllers)} PLC, {len(machines)} machine, "
            f"{len(warnings)} cảnh báo."
        ))
