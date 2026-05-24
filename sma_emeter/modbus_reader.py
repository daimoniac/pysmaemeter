import logging
import time
from typing import Dict, List, Optional

from pymodbus.client.sync import ModbusTcpClient

from sma_emeter.config import CONFIG, MODBUS_DEVICE_TYPE, MODBUS_REGISTERS_8001


class ModbusClientPool:
    """Keeps Modbus TCP clients open across collection ticks."""

    def __init__(self) -> None:
        self._modbus_config = CONFIG['modbus']
        self._clients: Dict[str, ModbusTcpClient] = {}

    def _host(self, ip_suffix: str) -> str:
        return f"{self._modbus_config['base_ip']}.{ip_suffix}"

    def _get_client(self, ip_suffix: str) -> ModbusTcpClient:
        if ip_suffix not in self._clients:
            host = self._host(ip_suffix)
            self._clients[ip_suffix] = ModbusTcpClient(host, port=self._modbus_config['port'])
        return self._clients[ip_suffix]

    def _connect(self, client: ModbusTcpClient, host: str) -> bool:
        if client.is_socket_open():
            return True
        if client.connect():
            return True
        raise ConnectionError(
            f"Connection to {host}:{self._modbus_config['port']} not possible"
        )

    def read_device(
        self, ip_suffix: str, device_type: int, retries: Optional[int] = None
    ) -> Optional[Dict[str, int]]:
        if device_type != MODBUS_DEVICE_TYPE:
            return None

        modbus_config = self._modbus_config
        effective_retries = retries if retries is not None else modbus_config['retries']
        unit_id = modbus_config['unit_id']
        host = self._host(ip_suffix)
        register_order = [addr for _, addr in MODBUS_REGISTERS_8001]

        for attempt in range(effective_retries):
            try:
                client = self._get_client(ip_suffix)
                self._connect(client, host)

                register_values: Dict[int, int] = {}
                failed_registers: List[int] = []

                for name, addr in MODBUS_REGISTERS_8001:
                    try:
                        result = client.read_holding_registers(
                            address=addr, count=2, unit=unit_id
                        )
                        if hasattr(result, 'isError') and result.isError():
                            logging.warning(
                                "Skipping invalid register %d from %s: %s", addr, host, result
                            )
                            failed_registers.append(addr)
                            continue
                        if not hasattr(result, 'registers'):
                            logging.warning(
                                "Skipping register %d from %s: invalid response %s",
                                addr,
                                host,
                                result,
                            )
                            failed_registers.append(addr)
                            continue
                        register_values[addr] = int(result.registers[1])
                        logging.debug(
                            "Reading register %s (%d) from %s: %d",
                            name,
                            addr,
                            host,
                            register_values[addr],
                        )
                    except Exception as e:
                        logging.warning("Skipping register %d from %s: %s", addr, host, e)
                        failed_registers.append(addr)

                if failed_registers:
                    raise RuntimeError(
                        f"Incomplete register set from {host}. Failed: {failed_registers}"
                    )

                return {
                    name: register_values[addr]
                    for name, addr in MODBUS_REGISTERS_8001
                    if not name.startswith('_')
                }

            except ConnectionError as e:
                self._drop_client(ip_suffix)
                logging.warning(
                    "Attempt %d/%d - connection failed for %s: %s",
                    attempt + 1,
                    effective_retries,
                    host,
                    e,
                )
                if attempt < effective_retries - 1:
                    time.sleep(modbus_config['retry_delay'])
                continue
            except Exception:
                logging.exception("Permanent error reading from %s", host)
                self._drop_client(ip_suffix)
                return None

        logging.error("Failed to read from %s after %d attempts", host, effective_retries)
        return None

    def _drop_client(self, ip_suffix: str) -> None:
        client = self._clients.pop(ip_suffix, None)
        if client:
            client.close()

    def close_all(self) -> None:
        for client in self._clients.values():
            client.close()
        self._clients.clear()
