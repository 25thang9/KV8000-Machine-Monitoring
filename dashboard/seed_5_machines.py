from django.db import transaction
from monitoring.models import Machine, PlcController, SignalMapping

plc = PlcController.objects.get(code="PLC01")

signals = [
    ("RUN",              "MR{n}00",  "BIT"),
    ("STOP",             "MR{n}01",  "BIT"),
    ("ALARM",            "MR{n}02",  "BIT"),
    ("AUTO_MODE",        "MR{n}03",  "BIT"),
    ("PRODUCTION_COUNT", "DM{n}000", "UINT16"),
    ("CYCLE_TIME_MS",    "DM{n}002", "UINT16"),
    ("ALARM_CODE",       "DM{n}004", "UINT16"),
    ("RECIPE_NO",        "DM{n}005", "UINT16"),
]

with transaction.atomic():

    # Tat cac may MOCK cu, khong xoa lich su
    Machine.objects.filter(
        code__startswith="MACHINE-"
    ).update(is_active=False)

    for n in range(1, 6):

        machine_code = f"MACHINE0{n}"

        machine, created = Machine.objects.update_or_create(
            code=machine_code,
            defaults={
                "name": f"Machine {n:02d}",
                "description": f"PLC test machine {n:02d}",
                "controller": plc,
                "is_active": True,
            },
        )

        for signal, address_pattern, data_type in signals:

            SignalMapping.objects.update_or_create(
                machine=machine,
                signal=signal,
                defaults={
                    "address": address_pattern.format(n=n),
                    "data_type": data_type,
                    "word_order": "LOW_HIGH",
                    "scale": 1,
                    "offset": 0,
                    "is_enabled": True,
                },
            )

print("DONE")

for machine in Machine.objects.filter(
    is_active=True,
    controller=plc
).order_by("code"):

    print(
        machine.code,
        machine.name,
        machine.controller.code,
        machine.signal_mappings.count(),
    )