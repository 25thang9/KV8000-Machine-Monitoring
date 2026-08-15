from __future__ import annotations

import socket
import threading
import time
import unittest
from dataclasses import dataclass
from decimal import Decimal

from collector.keyence_hostlink import KeyenceHostLinkClient


@dataclass
class Mapping:
    signal: str
    address: str
    data_type: str
    word_order: str = "LOW_HIGH"
    scale: Decimal = Decimal("1")
    offset: Decimal = Decimal("0")


class FakeHostLinkServer:
    def __init__(self, responses: dict[str, str], *, split_crlf: bool = False):
        self.responses = responses
        self.split_crlf = split_crlf
        self.commands: list[str] = []
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.host, self.port = self.sock.getsockname()
        self.sock.listen(1)
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass
        self.thread.join(timeout=1)

    def _run(self):
        try:
            conn, _addr = self.sock.accept()
        except OSError:
            return
        with conn:
            buffer = bytearray()
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                buffer.extend(chunk)
                while b"\r" in buffer:
                    pos = buffer.index(b"\r")
                    raw = bytes(buffer[:pos])
                    del buffer[: pos + 1]
                    command = raw.decode("ascii")
                    self.commands.append(command)
                    response = self.responses.get(command, "E1")
                    if self.split_crlf:
                        # Deliberately split exactly between CR and LF. This is
                        # the TCP boundary that can break naive line parsers.
                        conn.sendall((response + "\r").encode("ascii"))
                        time.sleep(0.02)
                        conn.sendall(b"\n")
                    else:
                        # Split payload to verify partial TCP recv handling.
                        encoded = (response + "\r\n").encode("ascii")
                        midpoint = max(1, len(encoded) // 2)
                        conn.sendall(encoded[:midpoint])
                        conn.sendall(encoded[midpoint:])


class HostLinkClientTests(unittest.TestCase):
    def test_single_word_read(self):
        server = FakeHostLinkServer({"RDS DM1000.U 1": "00123"}).start()
        try:
            with KeyenceHostLinkClient(server.host, server.port) as client:
                self.assertEqual(client.read_words("DM1000", 1), [123])
            self.assertEqual(server.commands, ["RDS DM1000.U 1"])
        finally:
            server.close()


    def test_crlf_split_between_packets_does_not_poison_next_command(self):
        server = FakeHostLinkServer(
            {
                "RDS DM1000.U 1": "00001",
                "RDS DM1001.U 1": "00002",
            },
            split_crlf=True,
        ).start()
        try:
            with KeyenceHostLinkClient(server.host, server.port) as client:
                self.assertEqual(client.read_words("DM1000", 1), [1])
                self.assertEqual(client.read_words("DM1001", 1), [2])
            self.assertEqual(
                server.commands,
                ["RDS DM1000.U 1", "RDS DM1001.U 1"],
            )
        finally:
            server.close()

    def test_batch_mappings_use_two_commands_for_contiguous_mr_and_dm(self):
        server = FakeHostLinkServer(
            {
                "RDS MR100 4": "1 0 0 1",
                "RDS DM1000.U 6": "10 0 250 0 301 7",
            }
        ).start()
        mappings = [
            Mapping("RUN", "MR100", "BIT"),
            Mapping("STOP", "MR101", "BIT"),
            Mapping("ALARM", "MR102", "BIT"),
            Mapping("AUTO_MODE", "MR103", "BIT"),
            Mapping("PRODUCTION_COUNT", "DM1000", "UINT16"),
            Mapping("CYCLE_TIME_MS", "DM1002", "UINT16"),
            Mapping("ALARM_CODE", "DM1004", "UINT16"),
            Mapping("RECIPE_NO", "DM1005", "UINT16"),
        ]
        try:
            with KeyenceHostLinkClient(server.host, server.port) as client:
                values = client.read_mappings(mappings)
            self.assertTrue(values["RUN"])
            self.assertFalse(values["STOP"])
            self.assertTrue(values["AUTO_MODE"])
            self.assertEqual(values["PRODUCTION_COUNT"], 10)
            self.assertEqual(values["CYCLE_TIME_MS"], 250)
            self.assertEqual(values["ALARM_CODE"], 301)
            self.assertEqual(values["RECIPE_NO"], 7)
            self.assertEqual(
                server.commands,
                ["RDS MR100 4", "RDS DM1000.U 6"],
            )
        finally:
            server.close()

    def test_uint32_low_high_and_scale(self):
        server = FakeHostLinkServer({"RDS DM200.U 2": "00002 00001"}).start()
        mapping = Mapping(
            "COUNT",
            "DM200",
            "UINT32",
            word_order="LOW_HIGH",
            scale=Decimal("2"),
        )
        try:
            with KeyenceHostLinkClient(server.host, server.port) as client:
                values = client.read_mappings([mapping])
            self.assertEqual(values["COUNT"], ((1 << 16) | 2) * 2)
        finally:
            server.close()


if __name__ == "__main__":
    unittest.main()
