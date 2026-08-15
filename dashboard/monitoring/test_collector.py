from __future__ import annotations

import socket
import tempfile
from pathlib import Path

from django.test import TransactionTestCase

from collector.plc_collector import (
    SingleInstanceError,
    SingleInstanceLock,
    _close_all_clients,
    poll_controller,
)
from collector.test_keyence_hostlink import FakeHostLinkServer
from monitoring.models import (
    Machine,
    MachineCurrentState,
    MachineReading,
    PlcController,
    SignalMapping,
)


DEFAULT_MAP = [
    (SignalMapping.Signal.RUN, "MR100", SignalMapping.DataType.BIT),
    (SignalMapping.Signal.STOP, "MR101", SignalMapping.DataType.BIT),
    (SignalMapping.Signal.ALARM, "MR102", SignalMapping.DataType.BIT),
    (SignalMapping.Signal.AUTO_MODE, "MR103", SignalMapping.DataType.BIT),
    (SignalMapping.Signal.PRODUCTION_COUNT, "DM1000", SignalMapping.DataType.UINT16),
    (SignalMapping.Signal.CYCLE_TIME_MS, "DM1002", SignalMapping.DataType.UINT16),
    (SignalMapping.Signal.ALARM_CODE, "DM1004", SignalMapping.DataType.UINT16),
    (SignalMapping.Signal.RECIPE_NO, "DM1005", SignalMapping.DataType.UINT16),
]


def add_mappings(machine: Machine) -> None:
    SignalMapping.objects.bulk_create(
        [
            SignalMapping(
                machine=machine,
                signal=signal,
                address=address,
                data_type=data_type,
            )
            for signal, address, data_type in DEFAULT_MAP
        ]
    )


class CollectorIntegrationTests(TransactionTestCase):
    def tearDown(self):
        _close_all_clients()
        super().tearDown()

    def make_controller(self, *, code: str, host: str, port: int):
        plc = PlcController.objects.create(
            code=code,
            name=code,
            host=host,
            port=port,
            poll_interval_ms=1000,
            connect_timeout_ms=200,
            read_timeout_ms=500,
            history_interval_seconds=30,
            offline_write_interval_seconds=30,
        )
        machine = Machine.objects.create(
            code=f"M-{code}",
            name=f"Machine {code}",
            controller=plc,
        )
        add_mappings(machine)
        return plc, machine

    def test_real_collector_poll_updates_current_state_and_history(self):
        server = FakeHostLinkServer(
            {
                "RDS MR100 4": "1 0 0 1",
                "RDS DM1000.U 6": "42 0 3150 0 0 7",
            }
        ).start()
        try:
            plc, machine = self.make_controller(
                code="PLC-01",
                host=server.host,
                port=server.port,
            )
            result = poll_controller(plc.id)

            self.assertTrue(result["online"])
            self.assertEqual(result["successful"], 1)
            state = MachineCurrentState.objects.get(machine=machine)
            self.assertTrue(state.plc_online)
            self.assertTrue(state.run_bit)
            self.assertTrue(state.auto_mode_bit)
            self.assertEqual(state.production_count, 42)
            self.assertEqual(state.cycle_time_ms, 3150)
            self.assertEqual(state.recipe_no, 7)
            self.assertEqual(state.last_error, "")

            history = MachineReading.objects.get(machine=machine)
            self.assertTrue(history.plc_online)
            self.assertEqual(history.production_count, 42)
        finally:
            server.close()

    def test_realtime_state_updates_without_writing_history_every_poll(self):
        server1 = FakeHostLinkServer(
            {
                "RDS MR100 4": "1 0 0 1",
                "RDS DM1000.U 6": "10 0 1000 0 0 1",
            }
        ).start()
        try:
            plc, machine = self.make_controller(
                code="PLC-LIVE", host=server1.host, port=server1.port
            )
            first = poll_controller(plc.id)
            self.assertTrue(first["online"])
            self.assertEqual(MachineReading.objects.filter(machine=machine).count(), 1)
        finally:
            server1.close()

        server2 = FakeHostLinkServer(
            {
                "RDS MR100 4": "1 0 0 1",
                "RDS DM1000.U 6": "11 0 1100 0 0 1",
            }
        ).start()
        try:
            plc.host = server2.host
            plc.port = server2.port
            plc.save(update_fields=["host", "port"])
            second = poll_controller(plc.id)
            self.assertTrue(second["online"])
            state = MachineCurrentState.objects.get(machine=machine)
            self.assertEqual(state.production_count, 11)
            self.assertEqual(state.cycle_time_ms, 1100)
            # Numeric-only changes are realtime in CurrentState but historical
            # sampling waits for history_interval_seconds.
            self.assertEqual(MachineReading.objects.filter(machine=machine).count(), 1)
        finally:
            server2.close()

    def test_offline_poll_preserves_last_numeric_values_and_resets_bits(self):
        # Reserve then close a localhost port so connect() deterministically fails.
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        host, port = sock.getsockname()
        sock.close()

        plc, machine = self.make_controller(code="PLC-OFF", host=host, port=port)
        MachineCurrentState.objects.create(
            machine=machine,
            plc_online=True,
            run_bit=True,
            stop_bit=False,
            alarm_bit=True,
            auto_mode_bit=True,
            production_count=1234,
            cycle_time_ms=2500,
            alarm_code=55,
            recipe_no=8,
        )
        MachineReading.objects.create(
            machine=machine,
            plc_online=True,
            run_bit=True,
            production_count=1234,
            cycle_time_ms=2500,
            alarm_code=55,
            recipe_no=8,
            source=MachineReading.DataSource.PLC,
        )

        result = poll_controller(plc.id)
        self.assertFalse(result["online"])

        state = MachineCurrentState.objects.get(machine=machine)
        self.assertFalse(state.plc_online)
        self.assertFalse(state.run_bit)
        self.assertFalse(state.stop_bit)
        self.assertFalse(state.alarm_bit)
        self.assertFalse(state.auto_mode_bit)
        self.assertEqual(state.production_count, 1234)
        self.assertEqual(state.cycle_time_ms, 2500)
        self.assertEqual(state.alarm_code, 55)
        self.assertEqual(state.recipe_no, 8)
        self.assertTrue(state.last_error)

        newest = MachineReading.objects.filter(machine=machine).order_by("-pk").first()
        self.assertFalse(newest.plc_online)
        self.assertEqual(newest.production_count, 1234)

    def test_same_controller_reuses_persistent_tcp_session(self):
        server = FakeHostLinkServer(
            {
                "RDS MR100 4": "1 0 0 1",
                "RDS DM1000.U 6": "5 0 1000 0 0 1",
            }
        ).start()
        try:
            plc, machine = self.make_controller(
                code="PLC-PERSIST", host=server.host, port=server.port
            )
            first = poll_controller(plc.id)
            second = poll_controller(plc.id)
            self.assertTrue(first["online"])
            self.assertTrue(second["online"])
            self.assertEqual(
                server.commands,
                [
                    "RDS MR100 4",
                    "RDS DM1000.U 6",
                    "RDS MR100 4",
                    "RDS DM1000.U 6",
                ],
            )
            self.assertEqual(
                MachineCurrentState.objects.get(machine=machine).production_count,
                5,
            )
        finally:
            _close_all_clients()
            server.close()

    def test_two_machines_on_one_plc_are_batched_by_device_span(self):
        server = FakeHostLinkServer(
            {
                "RDS MR100 8": "1 0 0 1 0 1 1 0",
                "RDS DM1000.U 12": "10 0 1000 0 0 1 20 0 2000 0 9 2",
            }
        ).start()
        try:
            plc = PlcController.objects.create(
                code="PLC-BATCH",
                name="PLC-BATCH",
                host=server.host,
                port=server.port,
                poll_interval_ms=250,
                connect_timeout_ms=200,
                read_timeout_ms=500,
            )
            m1 = Machine.objects.create(code="M-B1", name="M-B1", controller=plc)
            m2 = Machine.objects.create(code="M-B2", name="M-B2", controller=plc)
            add_mappings(m1)
            second_map = [
                (SignalMapping.Signal.RUN, "MR104", SignalMapping.DataType.BIT),
                (SignalMapping.Signal.STOP, "MR105", SignalMapping.DataType.BIT),
                (SignalMapping.Signal.ALARM, "MR106", SignalMapping.DataType.BIT),
                (SignalMapping.Signal.AUTO_MODE, "MR107", SignalMapping.DataType.BIT),
                (SignalMapping.Signal.PRODUCTION_COUNT, "DM1006", SignalMapping.DataType.UINT16),
                (SignalMapping.Signal.CYCLE_TIME_MS, "DM1008", SignalMapping.DataType.UINT16),
                (SignalMapping.Signal.ALARM_CODE, "DM1010", SignalMapping.DataType.UINT16),
                (SignalMapping.Signal.RECIPE_NO, "DM1011", SignalMapping.DataType.UINT16),
            ]
            SignalMapping.objects.bulk_create([
                SignalMapping(machine=m2, signal=sig, address=addr, data_type=dtype)
                for sig, addr, dtype in second_map
            ])

            result = poll_controller(plc.id)
            self.assertTrue(result["online"])
            self.assertEqual(result["successful"], 2)
            self.assertEqual(server.commands, ["RDS MR100 8", "RDS DM1000.U 12"])
            s1 = MachineCurrentState.objects.get(machine=m1)
            s2 = MachineCurrentState.objects.get(machine=m2)
            self.assertTrue(s1.run_bit)
            self.assertEqual(s1.production_count, 10)
            self.assertTrue(s2.stop_bit)
            self.assertTrue(s2.alarm_bit)
            self.assertEqual(s2.production_count, 20)
            self.assertEqual(s2.alarm_code, 9)
            self.assertEqual(s2.recipe_no, 2)
        finally:
            _close_all_clients()
            server.close()

    def test_two_independent_plcs_can_be_polled_from_database_config(self):
        server1 = FakeHostLinkServer(
            {"RDS MR100 4": "1 0 0 1", "RDS DM1000.U 6": "1 0 1000 0 0 1"}
        ).start()
        server2 = FakeHostLinkServer(
            {"RDS MR100 4": "0 1 0 0", "RDS DM1000.U 6": "2 0 2000 0 0 2"}
        ).start()
        try:
            plc1, machine1 = self.make_controller(code="PLC-A", host=server1.host, port=server1.port)
            plc2, machine2 = self.make_controller(code="PLC-B", host=server2.host, port=server2.port)

            result1 = poll_controller(plc1.id)
            result2 = poll_controller(plc2.id)
            self.assertTrue(result1["online"])
            self.assertTrue(result2["online"])
            self.assertEqual(MachineCurrentState.objects.get(machine=machine1).production_count, 1)
            self.assertEqual(MachineCurrentState.objects.get(machine=machine2).production_count, 2)
        finally:
            server1.close()
            server2.close()


class CollectorProcessSafetyTests(TransactionTestCase):
    def test_single_instance_lock_rejects_second_collector(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / "collector.lock"
            with SingleInstanceLock(lock_path):
                with self.assertRaises(SingleInstanceError):
                    with SingleInstanceLock(lock_path):
                        pass
