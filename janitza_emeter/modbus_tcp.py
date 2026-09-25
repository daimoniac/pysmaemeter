"""Minimal Modbus TCP holding-register client (stdlib only)."""

from __future__ import annotations

import socket
import struct
from typing import List, Optional


class ModbusTcpError(ConnectionError):
    """Raised when a Modbus TCP exchange fails."""


def _recv_exact(sock: socket.socket, nbytes: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < nbytes:
        piece = sock.recv(nbytes - len(chunks))
        if not piece:
            raise ModbusTcpError("connection closed during Modbus read")
        chunks.extend(piece)
    return bytes(chunks)


class StdlibModbusTcpClient:
    def __init__(self, host: str, port: int, unit_id: int, timeout: float) -> None:
        self.host = host
        self.port = int(port)
        self.unit_id = int(unit_id) & 0xFF
        self.timeout = float(timeout)
        self._sock: Optional[socket.socket] = None
        self._transaction_id = 0

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def _connect(self) -> socket.socket:
        if self._sock is not None:
            return self._sock
        sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        sock.settimeout(self.timeout)
        self._sock = sock
        return sock

    def read_holding_registers(self, address: int, count: int) -> List[int]:
        try:
            sock = self._connect()
            self._transaction_id = (self._transaction_id + 1) & 0xFFFF or 1
            pdu = struct.pack('>BHH', 3, int(address) & 0xFFFF, int(count) & 0xFFFF)
            mbap = struct.pack(
                '>HHHB', self._transaction_id, 0, 1 + len(pdu), self.unit_id
            )
            sock.sendall(mbap + pdu)

            header = _recv_exact(sock, 6)
            recv_tid, protocol, length = struct.unpack('>HHH', header)
            if protocol != 0:
                raise ModbusTcpError(f"unexpected protocol id {protocol}")
            if length < 3:
                raise ModbusTcpError(f"truncated Modbus TCP length {length}")
            rest = _recv_exact(sock, length)
            unit = rest[0]
            function = rest[1]
            body = rest[2:]
            if unit != self.unit_id:
                raise ModbusTcpError(f"unit id mismatch: expected {self.unit_id}, got {unit}")
            if recv_tid != self._transaction_id:
                raise ModbusTcpError(
                    f"transaction id mismatch: expected {self._transaction_id}, got {recv_tid}"
                )
            if function & 0x80:
                code = body[0] if body else -1
                raise ModbusTcpError(f"Modbus exception {code} for function {function & 0x7F}")
            if function != 3:
                raise ModbusTcpError(f"unexpected function {function}")
            byte_count = body[0]
            payload = body[1:]
            if byte_count != len(payload) or byte_count != count * 2:
                raise ModbusTcpError(
                    f"short Modbus payload: byte_count={byte_count} payload={len(payload)}"
                )
            return [
                struct.unpack('>H', payload[i:i + 2])[0] for i in range(0, byte_count, 2)
            ]
        except Exception:
            self.close()
            raise
