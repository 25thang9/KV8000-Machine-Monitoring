from datetime import timedelta

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.utils import timezone

from .models import (
    Machine,
    MachineCurrentState,
    MachineReading,
    PlcController,
    SignalMapping,
)
from .views import (
    _build_status_timeline,
    _latest_reading,
    _signal_rows,
    _snapshot,
)


class MachineStateTests(TestCase):
    def setUp(self):
        self.controller = PlcController.objects.create(
            code="PLC-TEST",
            name="Test PLC",
            host="192.168.0.10",
            history_interval_seconds=5,
        )
        self.machine = Machine.objects.create(
            code="TEST-01",
            name="Test machine",
            controller=self.controller,
        )

    def create_reading(self, **overrides):
        values = {
            "machine": self.machine,
            "recorded_at": timezone.now(),
            "plc_online": True,
            "run_bit": False,
            "stop_bit": False,
            "alarm_bit": False,
            "auto_mode_bit": True,
            "production_count": 10,
            "cycle_time_ms": 3500,
            "alarm_code": 0,
            "recipe_no": 1,
            "source": MachineReading.DataSource.PLC,
        }
        values.update(overrides)
        return MachineReading.objects.create(**values)

    def test_no_data_is_unknown_and_offline(self):
        snapshot = _snapshot(self.machine, None)
        self.assertEqual(snapshot.state, "unknown")
        self.assertEqual(snapshot.connection, "offline")

    def test_real_plc_offline_overrides_old_run_bit(self):
        reading = self.create_reading(plc_online=False, run_bit=True)
        snapshot = _snapshot(self.machine, reading)
        self.assertEqual(snapshot.state, "offline")
        self.assertEqual(snapshot.connection, "offline")

    def test_alarm_has_priority_over_run(self):
        reading = self.create_reading(run_bit=True, alarm_bit=True)
        snapshot = _snapshot(self.machine, reading)
        self.assertEqual(snapshot.state, "alarm")

    def test_fresh_run_is_run(self):
        reading = self.create_reading(run_bit=True)
        snapshot = _snapshot(self.machine, reading)
        self.assertEqual(snapshot.state, "run")
        self.assertEqual(snapshot.connection, "online")

    def test_run_and_stop_together_is_raised_as_alarm_state(self):
        reading = self.create_reading(run_bit=True, stop_bit=True, alarm_bit=False)
        snapshot = _snapshot(self.machine, reading)
        self.assertEqual(snapshot.state, "alarm")
        self.assertIn("RUN/STOP", snapshot.state_label)

    def test_stale_plc_data_is_offline(self):
        reading = self.create_reading(
            recorded_at=timezone.now() - timedelta(seconds=30),
            run_bit=True,
        )
        snapshot = _snapshot(self.machine, reading)
        self.assertEqual(snapshot.state, "offline")
        self.assertTrue(snapshot.is_stale)

    def test_timeline_current_minute_uses_aggregated_state(self):
        self.create_reading(run_bit=True)
        rows, _labels = _build_status_timeline([self.machine], minutes=3)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["segments"][-1]["state"], "run")

    def test_current_state_is_preferred_over_history_for_realtime(self):
        self.create_reading(run_bit=False, stop_bit=True, production_count=10)
        MachineCurrentState.objects.create(
            machine=self.machine,
            plc_online=True,
            run_bit=True,
            stop_bit=False,
            auto_mode_bit=True,
            production_count=99,
            cycle_time_ms=1200,
            source="PLC",
        )
        current = _latest_reading(self.machine)
        self.assertIsInstance(current, MachineCurrentState)
        self.assertTrue(current.run_bit)
        self.assertEqual(current.production_count, 99)

    def test_health_endpoint_hides_plc_host(self):
        now = timezone.now()
        self.controller.last_poll_at = now
        self.controller.last_seen_at = now
        self.controller.save(update_fields=["last_poll_at", "last_seen_at"])
        response = self.client.get("/health/")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["active_controllers"], 1)
        self.assertEqual(payload["controllers"][0]["code"], "PLC-TEST")
        self.assertNotIn("host", payload["controllers"][0])
        self.assertTrue(payload["controllers"][0]["collector_fresh"])
        self.assertTrue(payload["controllers"][0]["plc_online"])



class SignalMappingTests(TestCase):
    def setUp(self):
        self.controller = PlcController.objects.create(
            code="PLC-01",
            name="KV",
            host="192.168.0.10",
        )
        self.machine = Machine.objects.create(
            code="M-01",
            name="Machine 01",
            controller=self.controller,
        )

    def test_bit_signal_requires_bit_type(self):
        mapping = SignalMapping(
            machine=self.machine,
            signal=SignalMapping.Signal.RUN,
            address="MR100",
            data_type=SignalMapping.DataType.UINT16,
        )
        with self.assertRaises(ValidationError):
            mapping.full_clean()

    def test_numeric_signal_rejects_bit_type(self):
        mapping = SignalMapping(
            machine=self.machine,
            signal=SignalMapping.Signal.PRODUCTION_COUNT,
            address="DM1000",
            data_type=SignalMapping.DataType.BIT,
        )
        with self.assertRaises(ValidationError):
            mapping.full_clean()

    def test_address_is_normalized(self):
        mapping = SignalMapping(
            machine=self.machine,
            signal=SignalMapping.Signal.RUN,
            address="mr100",
            data_type=SignalMapping.DataType.BIT,
        )
        mapping.full_clean()
        self.assertEqual(mapping.address, "MR100")

    def test_signal_rows_use_database_mapping(self):
        SignalMapping.objects.create(
            machine=self.machine,
            signal=SignalMapping.Signal.RUN,
            address="MR500",
            data_type=SignalMapping.DataType.BIT,
        )
        reading = MachineReading.objects.create(
            machine=self.machine,
            plc_online=True,
            run_bit=True,
            source=MachineReading.DataSource.PLC,
        )
        rows = _signal_rows(self.machine, reading)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["address"], "MR500")
        self.assertEqual(rows[0]["display_value"], "ON")


class PlcControllerValidationTests(TestCase):
    def test_invalid_port_and_poll_interval_are_rejected(self):
        plc = PlcController(
            code="BAD",
            name="Bad PLC",
            host="127.0.0.1",
            port=70000,
            poll_interval_ms=50,
        )
        with self.assertRaises(ValidationError):
            plc.full_clean()


class OptionalLoginMiddlewareTests(TestCase):
    @override_settings(MONITOR_REQUIRE_LOGIN=True)
    def test_dashboard_redirects_to_login_but_health_stays_public(self):
        dashboard_response = self.client.get("/")
        self.assertEqual(dashboard_response.status_code, 302)
        self.assertTrue(dashboard_response.url.startswith("/login/?next="))

        health_response = self.client.get("/health/")
        self.assertEqual(health_response.status_code, 200)


class PageSmokeTests(TestCase):
    def setUp(self):
        self.controller = PlcController.objects.create(
            code="PLC-WEB",
            name="Web PLC",
            host="127.0.0.1",
        )
        self.machine = Machine.objects.create(
            code="WEB-01",
            name="Web Machine",
            controller=self.controller,
        )
        mappings = [
            (SignalMapping.Signal.RUN, "MR500", SignalMapping.DataType.BIT),
            (SignalMapping.Signal.STOP, "MR501", SignalMapping.DataType.BIT),
            (SignalMapping.Signal.ALARM, "MR502", SignalMapping.DataType.BIT),
            (SignalMapping.Signal.AUTO_MODE, "MR503", SignalMapping.DataType.BIT),
            (SignalMapping.Signal.PRODUCTION_COUNT, "DM5000", SignalMapping.DataType.UINT16),
            (SignalMapping.Signal.CYCLE_TIME_MS, "DM5002", SignalMapping.DataType.UINT16),
            (SignalMapping.Signal.ALARM_CODE, "DM5004", SignalMapping.DataType.UINT16),
            (SignalMapping.Signal.RECIPE_NO, "DM5005", SignalMapping.DataType.UINT16),
        ]
        SignalMapping.objects.bulk_create([
            SignalMapping(machine=self.machine, signal=sig, address=addr, data_type=dtype)
            for sig, addr, dtype in mappings
        ])
        MachineCurrentState.objects.create(
            machine=self.machine,
            plc_online=True,
            run_bit=True,
            auto_mode_bit=True,
            production_count=88,
            cycle_time_ms=1500,
            recipe_no=3,
        )
        MachineReading.objects.create(
            machine=self.machine,
            plc_online=True,
            run_bit=True,
            auto_mode_bit=True,
            production_count=88,
            cycle_time_ms=1500,
            recipe_no=3,
            source=MachineReading.DataSource.PLC,
        )

    @override_settings(MONITOR_REQUIRE_LOGIN=False)
    def test_main_pages_render(self):
        urls = [
            "/",
            "/machines/WEB-01/",
            "/alarms/",
            "/history/?machine=WEB-01",
            "/system/",
            "/health/",
            "/login/",
        ]
        for url in urls:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)

    @override_settings(MONITOR_REQUIRE_LOGIN=False)
    def test_realtime_json_endpoint_returns_current_state(self):
        response = self.client.get("/api/dashboard-state/")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["summary"]["run"], 1)
        self.assertEqual(payload["machines"][0]["code"], "WEB-01")
        self.assertTrue(payload["machines"][0]["run_bit"])

    @override_settings(MONITOR_REQUIRE_LOGIN=False)
    def test_realtime_sse_stream_starts_with_state_event(self):
        response = self.client.get("/api/dashboard-stream/")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.streaming)
        iterator = iter(response.streaming_content)
        first = next(iterator).decode("utf-8")
        second = next(iterator).decode("utf-8")
        self.assertIn("retry:", first)
        self.assertIn("event: state", second)
        self.assertIn('"WEB-01"', second)
        response.close()

    @override_settings(MONITOR_REQUIRE_LOGIN=False)
    def test_machine_detail_uses_database_addresses_not_old_hardcode(self):
        response = self.client.get("/machines/WEB-01/")
        self.assertContains(response, "DM5000")
        self.assertContains(response, "DM5002")
        self.assertContains(response, "MR503")
