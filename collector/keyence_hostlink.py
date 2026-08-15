from __future__ import annotations

import re
import socket
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable, Protocol


DEVICE_RE = re.compile(r"^([A-Z]{1,3})(\d+)$")
MAX_RESPONSE_BYTES = 64 * 1024


class HostLinkError(RuntimeError):
    """Base exception for Host Link communication errors."""


class HostLinkProtocolError(HostLinkError):
    """PLC returned an invalid or unexpected Host Link response."""


class HostLinkConnectionError(HostLinkError):
    """TCP connection disappeared while waiting for a Host Link response."""


class MappingLike(Protocol):
    signal: str
    address: str
    data_type: str
    word_order: str
    scale: Decimal
    offset: Decimal


@dataclass(frozen=True)
class DeviceAddress:
    prefix: str
    number: int

    @classmethod
    def parse(cls, address: str) -> "DeviceAddress":
        normalized = (address or "").strip().upper()
        match = DEVICE_RE.fullmatch(normalized)
        if not match:
            raise HostLinkProtocolError(
                f"Device address không được hỗ trợ bởi collector: {address!r}. "
                "Dùng global device dạng MR100, DM1000, ..."
            )
        return cls(prefix=match.group(1), number=int(match.group(2)))

    def format(self, number: int | None = None) -> str:
        return f"{self.prefix}{self.number if number is None else number}"


@dataclass
class _SpanItem:
    mapping: MappingLike
    address: DeviceAddress
    width: int


class KeyenceHostLinkClient:
    """Read-only KEYENCE Host Link TCP client.

    Collector chỉ phát lệnh RDS. Không có API ghi device PLC trong class này.
    """

    def __init__(
        self,
        host: str,
        port: int = 8501,
        *,
        connect_timeout: float = 2.0,
        read_timeout: float = 2.0,
    ) -> None:
        self.host = host
        self.port = int(port)
        self.connect_timeout = float(connect_timeout)
        self.read_timeout = float(read_timeout)
        self._socket: socket.socket | None = None
        self._rx_buffer = bytearray()

    def __enter__(self) -> "KeyenceHostLinkClient":
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def connect(self) -> None:
        self.close()
        sock = socket.create_connection(
            (self.host, self.port),
            timeout=self.connect_timeout,
        )
        sock.settimeout(self.read_timeout)
        self._socket = sock
        self._rx_buffer.clear()

    def close(self) -> None:
        sock = self._socket
        self._socket = None
        self._rx_buffer.clear()
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    def _require_socket(self) -> socket.socket:
        if self._socket is None:
            raise HostLinkError("Host Link socket chưa được kết nối.")
        return self._socket

    def _recv_line(self) -> str:
        """Receive one Host Link response line.

        KV-7500 thực tế trả CRLF. Không coi một byte CR đứng ở cuối packet
        là kết thúc dòng ngay vì TCP có thể tách ``\r`` và ``\n`` sang hai
        packet khác nhau; nếu xử lý CR quá sớm, byte LF còn lại sẽ làm lệnh
        kế tiếp nhận một response rỗng.
        """
        sock = self._require_socket()

        while True:
            lf_pos = self._rx_buffer.find(b"\n")
            if lf_pos >= 0:
                raw = bytes(self._rx_buffer[:lf_pos])
                del self._rx_buffer[: lf_pos + 1]
                if raw.endswith(b"\r"):
                    raw = raw[:-1]
                return raw.decode("ascii", errors="strict").strip()

            if len(self._rx_buffer) >= MAX_RESPONSE_BYTES:
                raise HostLinkProtocolError("Response Host Link vượt giới hạn an toàn.")

            chunk = sock.recv(4096)
            if not chunk:
                raise HostLinkConnectionError(
                    "PLC đóng socket trước khi trả đủ một dòng Host Link."
                )
            self._rx_buffer.extend(chunk)

    def command(self, command: str) -> str:
        if "\r" in command or "\n" in command:
            raise ValueError("Command không được chứa CR/LF.")
        sock = self._require_socket()
        sock.sendall((command + "\r").encode("ascii"))
        return self._recv_line()

    @staticmethod
    def _parse_integer_tokens(response: str, expected_count: int) -> list[int]:
        tokens = response.split()
        if len(tokens) != expected_count:
            raise HostLinkProtocolError(
                f"PLC trả {len(tokens)} giá trị, cần {expected_count}: {response!r}"
            )

        values: list[int] = []
        for token in tokens:
            try:
                values.append(int(token, 10))
            except ValueError as exc:
                raise HostLinkProtocolError(
                    f"Response Host Link không phải số: {response!r}"
                ) from exc
        return values

    def read_words(self, address: str, count: int = 1) -> list[int]:
        if count <= 0:
            raise ValueError("count phải > 0")
        parsed = DeviceAddress.parse(address)
        response = self.command(f"RDS {parsed.format()}.U {count}")
        values = self._parse_integer_tokens(response, count)
        for value in values:
            if not 0 <= value <= 0xFFFF:
                raise HostLinkProtocolError(
                    f"Word unsigned ngoài miền 0..65535: {value}"
                )
        return values

    def read_bits(self, address: str, count: int = 1) -> list[int]:
        if count <= 0:
            raise ValueError("count phải > 0")
        parsed = DeviceAddress.parse(address)
        response = self.command(f"RDS {parsed.format()} {count}")
        values = self._parse_integer_tokens(response, count)
        if any(value not in (0, 1) for value in values):
            raise HostLinkProtocolError(
                f"Bit response phải là 0/1: {response!r}"
            )
        return values

    @staticmethod
    def _mapping_width(data_type: str) -> int:
        return 2 if data_type in {"UINT32", "INT32"} else 1

    @staticmethod
    def _convert_words(mapping: MappingLike, words: list[int]) -> int:
        data_type = mapping.data_type
        if data_type == "UINT16":
            raw = words[0]
        elif data_type == "INT16":
            raw = words[0]
            if raw >= 0x8000:
                raw -= 0x10000
        elif data_type in {"UINT32", "INT32"}:
            if len(words) != 2:
                raise HostLinkProtocolError("Giá trị 32-bit phải có đúng 2 word.")
            first, second = words
            if getattr(mapping, "word_order", "LOW_HIGH") == "HIGH_LOW":
                high, low = first, second
            else:
                low, high = first, second
            raw = (high << 16) | low
            if data_type == "INT32" and raw >= 0x80000000:
                raw -= 0x100000000
        else:
            raise HostLinkProtocolError(f"Data type không hỗ trợ: {data_type}")

        scaled = (
            Decimal(raw) * Decimal(mapping.scale)
            + Decimal(mapping.offset)
        )
        return int(scaled.to_integral_value(rounding=ROUND_HALF_UP))

    def read_mappings(self, mappings: Iterable[MappingLike]) -> dict[str, int | bool]:
        """Read mappings with contiguous batching.

        Các BIT liên tiếp (ví dụ MR100..MR103) được gộp thành một RDS.
        Các WORD liên tiếp (ví dụ DM1000..DM1005) cũng được gộp thành một RDS.
        """
        items: list[_SpanItem] = []
        for mapping in mappings:
            address = DeviceAddress.parse(mapping.address)
            width = self._mapping_width(mapping.data_type)
            items.append(_SpanItem(mapping=mapping, address=address, width=width))

        results: dict[str, int | bool] = {}
        bit_items = [item for item in items if item.mapping.data_type == "BIT"]
        word_items = [item for item in items if item.mapping.data_type != "BIT"]

        self._read_spans(bit_items, results, is_bit=True)
        self._read_spans(word_items, results, is_bit=False)
        return results

    def _read_spans(
        self,
        items: list[_SpanItem],
        results: dict[str, int | bool],
        *,
        is_bit: bool,
    ) -> None:
        groups: dict[str, list[_SpanItem]] = {}
        for item in items:
            groups.setdefault(item.address.prefix, []).append(item)

        for prefix, group in groups.items():
            group.sort(key=lambda item: item.address.number)
            spans: list[list[_SpanItem]] = []
            current: list[_SpanItem] = []
            current_end: int | None = None

            for item in group:
                item_start = item.address.number
                item_end = item_start + item.width - 1
                candidate_start = current[0].address.number if current else item_start
                candidate_end = max(current_end or item_end, item_end)
                gap_ok = current_end is not None and item_start <= current_end + 3
                span_ok = (candidate_end - candidate_start + 1) <= 64
                if not current or (gap_ok and span_ok):
                    current.append(item)
                    current_end = item_end if current_end is None else max(current_end, item_end)
                else:
                    spans.append(current)
                    current = [item]
                    current_end = item_end
            if current:
                spans.append(current)

            for span in spans:
                start = min(item.address.number for item in span)
                end = max(item.address.number + item.width - 1 for item in span)
                count = end - start + 1
                start_address = f"{prefix}{start}"
                raw_values = (
                    self.read_bits(start_address, count)
                    if is_bit
                    else self.read_words(start_address, count)
                )

                for item in span:
                    offset = item.address.number - start
                    segment = raw_values[offset : offset + item.width]
                    if is_bit:
                        results[item.mapping.signal] = bool(segment[0])
                    else:
                        results[item.mapping.signal] = self._convert_words(
                            item.mapping,
                            segment,
                        )
