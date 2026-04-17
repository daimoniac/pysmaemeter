#!/usr/bin/env python3
# coding=utf-8
import time
import schedule
import asyncio
import logging
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional, Any
from pymodbus.client.sync import ModbusTcpClient
from lib.emeter import emeterPacket
import socket

# Configure basic logging first, before any other operations
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler()
    ]
)

# Load configuration from config.json
def load_config() -> Dict[str, Any]:
    """Load configuration from config.json file"""
    config_path = Path(__file__).parent / 'config.json'
    try:
        with open(config_path, 'r') as f:
            config = json.load(f)
        logging.info(f"Configuration loaded from {config_path}")
        return config
    except FileNotFoundError:
        logging.error(f"Configuration file not found: {config_path}")
        sys.exit(1)
    except json.JSONDecodeError as e:
        logging.error(f"Invalid JSON in config file: {e}")
        sys.exit(1)


# Load configuration
try:
    CONFIG = load_config()
except Exception as e:
    logging.error(f"Failed to load configuration: {e}")
    sys.exit(1)

TOTAL_COUNTER_PATH = Path(__file__).parent / 'total_counter.json'

# Convenience reference for devices and modbus registers
SMA_DEVICES: Dict[str, Dict[str, Any]] = CONFIG['devices']
REGISTERS: Dict[int, List[int]] = {
    8001: [30773, 30961, 30775, 30535, 30777, 30779, 30781]
  }

# SMA scaling factors for emeterPacket
SCALING_FACTORS = {
    'power': 10,      # Power (W) scale factor: store value * 10
    'energy': 3600    # Energy (Wh) to joules conversion: store value * 3600
}

# Some inverter firmwares intermittently report this sentinel as daily yield.
INVALID_DAILY_YIELD_VALUES = {65535}

# Update logging level from config if specified
logging_config = CONFIG.get('logging', {})
if logging_config:
    logging.getLogger().setLevel(getattr(logging, logging_config.get('level', 'INFO'), logging.INFO))


def _current_date() -> str:
    """Returns the local date string in YYYY-MM-DD format."""
    return time.strftime('%Y-%m-%d', time.localtime())


def _parse_non_negative_int(value: Any, name: str) -> int:
    """Converts a value to non-negative int or raises ValueError."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number")
    if parsed < 0:
        raise ValueError(f"{name} must be >= 0")
    return parsed


def load_total_counter_state() -> Dict[str, Any]:
    """Loads persisted total counter state from disk with safe defaults."""
    default_state = {
        'date': _current_date(),
        'day_max_wh': 0,
        'accumulated_wh': 0
    }

    if not TOTAL_COUNTER_PATH.exists():
        return default_state

    try:
        with open(TOTAL_COUNTER_PATH, 'r') as f:
            raw = json.load(f)

        state = {
            'date': str(raw.get('date', default_state['date'])),
            'day_max_wh': _parse_non_negative_int(raw.get('day_max_wh', 0), 'day_max_wh'),
            'accumulated_wh': _parse_non_negative_int(raw.get('accumulated_wh', 0), 'accumulated_wh')
        }
        return state
    except Exception as e:
        logging.warning(f"Invalid total counter state file {TOTAL_COUNTER_PATH}: {e}. Resetting state.")
        return default_state


def save_total_counter_state(state: Dict[str, Any]) -> None:
    """Saves total counter state atomically to avoid partial writes."""
    tmp_path = TOTAL_COUNTER_PATH.with_suffix('.json.tmp')
    serialized = {
        'date': str(state['date']),
        'day_max_wh': _parse_non_negative_int(state['day_max_wh'], 'day_max_wh'),
        'accumulated_wh': _parse_non_negative_int(state['accumulated_wh'], 'accumulated_wh')
    }

    with open(tmp_path, 'w') as f:
        json.dump(serialized, f, indent=2)
        f.write('\n')

    tmp_path.replace(TOTAL_COUNTER_PATH)


def load_total_yield_baseline_wh() -> int:
    """Loads baseline total yield from config key emeter.totalyieldbaseline (kWh)."""
    emeter_config = CONFIG.get('emeter')
    if not isinstance(emeter_config, dict):
        raise ValueError("Missing emeter configuration")

    if 'totalyieldbaseline' not in emeter_config:
        raise ValueError("Missing config key emeter.totalyieldbaseline")

    try:
        baseline_kwh = float(emeter_config['totalyieldbaseline'])
    except (TypeError, ValueError):
        raise ValueError("Config key emeter.totalyieldbaseline must be numeric")

    if baseline_kwh < 0:
        raise ValueError("Config key emeter.totalyieldbaseline must be >= 0")

    # Config baseline is in kWh; internal logic uses Wh.
    return int(baseline_kwh * 1000)


def distribute_phase_values(total_value: int, p1: int, p2: int, p3: int) -> tuple[int, int, int]:
    """Distribute a total value across three phases proportionally to their power values.
    
    Args:
        total_value: Total value to distribute (e.g., daily_yield)
        p1, p2, p3: Phase power values used for proportional distribution
    
    Returns:
        Tuple of (p1_value, p2_value, p3_value)
    """
    total_power = p1 + p2 + p3
    if total_power > 0:
        p1_value = int((p1 / total_power) * total_value)
        p2_value = int((p2 / total_power) * total_value)
        p3_value = total_value - p1_value - p2_value  # Remainder to p3
    else:
        # No power, distribute evenly
        p1_value = total_value // 3
        p2_value = total_value // 3
        p3_value = total_value - p1_value - p2_value
    return p1_value, p2_value, p3_value


def sanitize_daily_yield(value: Any, device_label: str) -> int:
    """Normalize known invalid daily-yield values to 0."""
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        logging.warning(f"Invalid daily yield for {device_label}: {value!r}. Using 0.")
        return 0

    if normalized in INVALID_DAILY_YIELD_VALUES:
        logging.warning(
            f"Discarding invalid daily yield {normalized} for {device_label}. Using 0."
        )
        return 0

    return normalized


def _update_rollover_state(state: Dict[str, Any], raw_daily_yield_wh: int, baseline_wh: int) -> tuple[int, int]:
    """Update daily rollover counters and compute effective yield totals.

    Mutates *state* in place and persists to disk when changed.

    Returns:
        (current_daily_yield_wh, total_emitted_yield_wh) in Wh.
    """
    current_date = _current_date()
    current_daily_yield_wh = max(0, sanitize_daily_yield(raw_daily_yield_wh, 'aggregate daily_yield'))
    state_changed = False

    # Midnight rollover: add previous day max once and start a fresh day.
    # On the rollover cycle, discard the current daily yield reading
    # because inverters may not have reset their daily counters yet,
    # causing a stale reading to be captured as the new day's max.
    if state['date'] != current_date:
        previous_day = state['date']
        previous_day_max_wh = state['day_max_wh']
        state['accumulated_wh'] += previous_day_max_wh
        state['date'] = current_date
        state['day_max_wh'] = 0
        state_changed = True
        current_daily_yield_wh = 0
        logging.info(
            f"Daily rollover {previous_day} -> {current_date}: "
            f"added {previous_day_max_wh}Wh to accumulated total "
            f"({state['accumulated_wh']}Wh)"
        )
    elif current_daily_yield_wh > state['day_max_wh']:
        state['day_max_wh'] = current_daily_yield_wh
        state_changed = True

    if state_changed:
        save_total_counter_state(state)

    # Use the higher of current daily yield or day_max to prevent
    # the total counter going backwards when inverters reset their
    # daily counters at local midnight (before our server rollover).
    effective_daily_yield_wh = max(current_daily_yield_wh, state['day_max_wh'])
    total_emitted_yield_wh = baseline_wh + state['accumulated_wh'] + effective_daily_yield_wh
    logging.debug(
        f"Total emitted yield: baseline={baseline_wh}Wh, "
        f"accumulated={state['accumulated_wh']}Wh, "
        f"today={current_daily_yield_wh}Wh, effective={effective_daily_yield_wh}Wh, "
        f"emitted={total_emitted_yield_wh}Wh"
    )
    return current_daily_yield_wh, total_emitted_yield_wh


def is_snooze_time() -> bool:
    """Returns True if the current local time falls within the configured snooze window.

    The window is defined by 'snooze.start' and 'snooze.end' in config.json (HH:MM format).
    Ranges that wrap midnight (e.g. 23:00–03:00) are handled correctly.
    Returns False if no snooze section is configured.
    """
    snooze_config = CONFIG.get('snooze')
    if not snooze_config:
        return False

    try:
        start_str = snooze_config['start']
        end_str = snooze_config['end']
        start_h, start_m = map(int, start_str.split(':'))
        end_h, end_m = map(int, end_str.split(':'))
    except (KeyError, ValueError):
        logging.warning("Invalid snooze configuration; snooze disabled.")
        return False

    now = time.localtime()
    current_minutes = now.tm_hour * 60 + now.tm_min
    start_minutes = start_h * 60 + start_m
    end_minutes = end_h * 60 + end_m

    if start_minutes <= end_minutes:
        # Simple same-day range
        return start_minutes <= current_minutes < end_minutes
    else:
        # Wraps midnight (e.g. 23:00 – 03:00)
        return current_minutes >= start_minutes or current_minutes < end_minutes


def validate_configuration() -> None:
    """Validate device configuration at startup"""
    required_device_fields = {'type', 'name'}
    
    for device_id, device_info in SMA_DEVICES.items():
        # Check required fields
        if not isinstance(device_info, dict):
            raise ValueError(f"Device {device_id}: configuration must be a dictionary")
        
        missing_fields = required_device_fields - set(device_info.keys())
        if missing_fields:
            raise ValueError(f"Device {device_id}: missing required fields {missing_fields}")
        
        # Validate device type (8001 = Modbus inverter, 9999 = Speedwire)
        if device_info['type'] not in (8001, 9999):
            raise ValueError(f"Device {device_id}: unsupported device type {device_info['type']}")

    emeter_config = CONFIG.get('emeter')
    if not isinstance(emeter_config, dict):
        raise ValueError("Configuration: 'emeter' section missing or invalid")
    if 'totalyieldbaseline' not in emeter_config:
        raise ValueError("Configuration: missing emeter.totalyieldbaseline (kWh)")
    
    logging.info(f"Configuration validated: {len(SMA_DEVICES)} devices configured")


async def get_speedwire_data() -> Dict[str, int]:
    """Fetches Speedwire data with error handling and timeout"""
    try:
        from lib.speedwire_multigate_asyncio import fetch_speedwire_data
        data = await asyncio.wait_for(fetch_speedwire_data(), timeout=CONFIG['speedwire']['timeout'])
        return data
    except asyncio.TimeoutError:
        logging.error("Timeout fetching Speedwire data")
        return {'spotacpower': 0, 'tagesertrag': 0}
    except Exception as e:
        logging.error(f"Error fetching Speedwire data: {e}")
        return {'spotacpower': 0, 'tagesertrag': 0}


def get_values_persistent(ip_addr: str, sma_class: int, retries: Optional[int] = None) -> Optional[List[int]]:
    """
    Reads the registers defined in 'REGISTERS[sma_class]' from the configured base IP.<ip_addr>
    and returns a list of values in the exact register order. Uses retry logic.

    Important: partial reads are treated as invalid to avoid shifted index mapping
    (e.g. daily_yield accidentally reading a phase-power register).
    """
    modbus_config = CONFIG['modbus']
    effective_retries = retries if retries is not None else modbus_config['retries']

    register_order = REGISTERS[sma_class]
    unit_id = modbus_config['unit_id']
    host = f"{modbus_config['base_ip']}.{ip_addr}"

    for attempt in range(effective_retries):
        client = None
        try:
            client = ModbusTcpClient(host, port=modbus_config['port'])
            if not client.connect():
                raise ConnectionError(f"Connection to {host}:{modbus_config['port']} not possible")

            register_values: Dict[int, int] = {}
            failed_registers: List[int] = []

            for addr in register_order:
                try:
                    result = client.read_holding_registers(address=addr, count=2, unit=unit_id)
                    if hasattr(result, 'isError') and result.isError():
                        logging.warning(f"Skipping invalid register {addr} from {host}: {result}")
                        failed_registers.append(addr)
                        continue
                    if not hasattr(result, 'registers'):
                        logging.warning(f"Skipping register {addr} from {host}: Invalid response {result}")
                        failed_registers.append(addr)
                        continue

                    register_values[addr] = int(result.registers[1])
                    logging.debug(f"Reading register {addr} from {host} result: {register_values[addr]}")
                except Exception as e:
                    logging.warning(f"Skipping register {addr} from {host}: {e}")
                    failed_registers.append(addr)
                    continue

            if failed_registers:
                raise Exception(
                    f"Incomplete register set from {host}. Failed registers: {failed_registers}"
                )

            ordered_values = [register_values[addr] for addr in register_order]
            logging.debug(f"Successfully read {len(ordered_values)} registers from {host}")
            return ordered_values

        except ConnectionError as e:
            logging.warning(f"Attempt {attempt + 1}/{effective_retries} - Connection failed for {host}: {e}")
            if attempt < effective_retries - 1:
                time.sleep(modbus_config['retry_delay'])
            continue

        except Exception as e:
            logging.error(f"Permanent error reading from {host}: {e}")
            return None

        finally:
            if client:
                client.close()

    logging.error(f"Failed to read from {host} after {effective_retries} attempts")
    return None


def collect_speedwire_data_sync() -> Dict[str, int]:
    """Synchronous wrapper function for Speedwire data collection"""
    try:
        return asyncio.run(get_speedwire_data())
    except Exception as e:
        logging.error(f"Error collecting Speedwire data: {e}")
        return {'spotacpower': 0, 'tagesertrag': 0}


def _extract_phase_data(total_power: int, daily_yield: int, phase_power_data: Dict[str, int], device_label: str = '') -> Dict[str, int]:
    """Extract and calculate phase data from raw device data."""
    # Get phase powers (with fallback to equal distribution)
    p1_power = phase_power_data.get('p1_power', 0) or total_power // 3
    p2_power = phase_power_data.get('p2_power', 0) or total_power // 3
    p3_power = phase_power_data.get('p3_power', 0) or (total_power - p1_power - p2_power)

    # Get phase yields (calculate if not provided)
    if all(phase_power_data.get(f'p{i}_yield') is not None for i in [1, 2, 3]):
        p1_yield = sanitize_daily_yield(phase_power_data['p1_yield'], f"{device_label} p1_yield")
        p2_yield = sanitize_daily_yield(phase_power_data['p2_yield'], f"{device_label} p2_yield")
        p3_yield = sanitize_daily_yield(phase_power_data['p3_yield'], f"{device_label} p3_yield")
    else:
        p1_yield, p2_yield, p3_yield = distribute_phase_values(daily_yield, p1_power, p2_power, p3_power)

    return {
        'p1_power': p1_power, 'p1_yield': p1_yield,
        'p2_power': p2_power, 'p2_yield': p2_yield,
        'p3_power': p3_power, 'p3_yield': p3_yield
    }


def collect_data() -> Dict[str, Any]:
    """Collects data from all configured devices"""
    data_collection: Dict[str, Any] = {}
    
    for device_id, device_info in SMA_DEVICES.items():
        try:
            device_label = f"device {device_id} ({device_info['name']})"

            if device_info['type'] == 8001:
                # Inverter data via Modbus
                data = get_values_persistent(device_id, device_info['type'])
                if data is None:
                    logging.warning(f"No data from {device_label}")
                    continue
                
                # Registers: [30773, 30961, 30775, 30535, 30777, 30779, 30781]
                total_power = data[2]     # 30775: Total AC power
                daily_yield = sanitize_daily_yield(data[3], device_label)
                phase_power_data = {
                    'p1_power': data[4],  # 30777: Power L1
                    'p2_power': data[5],  # 30779: Power L2
                    'p3_power': data[6]   # 30781: Power L3
                }

            elif device_info['type'] == 9999:
                # Speedwire data
                data = collect_speedwire_data_sync()
                total_power = data.get('spotacpower', 0)
                daily_yield = sanitize_daily_yield(data.get('tagesertrag', 0), device_label)
                phase_power_data = data

            else:
                continue

            phase_data = _extract_phase_data(total_power, daily_yield, phase_power_data, device_label)
            data_collection[device_id] = {
                'total_power': total_power,
                'daily_yield': daily_yield,
                **phase_data
            }

        except Exception as e:
            logging.error(f"Error collecting data for device {device_id}: {e}")
            continue

    # Calculate aggregate totals per phase
    aggregate = {}
    for n in (1, 2, 3):
        for metric in ('power', 'yield'):
            key = f'p{n}_{metric}'
            aggregate[key] = sum(d.get(key, 0) for d in data_collection.values())

    aggregate['total_power'] = sum(aggregate[f'p{n}_power'] for n in (1, 2, 3))
    aggregate['daily_yield'] = sum(aggregate[f'p{n}_yield'] for n in (1, 2, 3))
    
    data_collection['aggregate'] = aggregate

    return data_collection


def add_phase_measurements(packet: emeterPacket, phase_suffix: str, power: int, energy: int) -> None:
    """Helper to add power and energy measurements for a single phase.
    
    Args:
        packet: The emeterPacket to add measurements to
        phase_suffix: Phase identifier (empty string for total, '_L1', '_L2', or '_L3')
        power: Power value in watts
        energy: Energy value in watt-hours
    
    Note: Positive values (consume) are always 0 for PV inverters.
          Negative values (supply) represent PV production.
    """
    # Positive (consume) - always 0 for PV production
    packet.addMeasurementValue(getattr(emeterPacket, f'SMA_POSITIVE_ACTIVE_POWER{phase_suffix}'), 0)
    packet.addCounterValue(getattr(emeterPacket, f'SMA_POSITIVE_ACTIVE_ENERGY{phase_suffix}'), 0)
    
    # Negative (supply) - PV production with SMA scaling
    packet.addMeasurementValue(getattr(emeterPacket, f'SMA_NEGATIVE_ACTIVE_POWER{phase_suffix}'), 
                               int(power * SCALING_FACTORS['power']))
    packet.addCounterValue(getattr(emeterPacket, f'SMA_NEGATIVE_ACTIVE_ENERGY{phase_suffix}'), 
                          int(energy * SCALING_FACTORS['energy']))


def build_emeter_packet(power: int, energy: int, p1_power: int = 0, p1_yield: int = 0,
                        p2_power: int = 0, p2_yield: int = 0, p3_power: int = 0, p3_yield: int = 0,
                        total_negative_active_energy: Optional[float] = None) -> bytes:
    """Build an emeterPacket and return the raw packet bytes.

    SMA convention:
    - Positive (consume) = power drawn from grid
    - Negative (supply) = power fed into grid
    For PV inverters, we're supplying power, so we use negative values.

    SMA scaling factors (from SCALING_FACTORS constant):
    - Power (W): store value * 10
    - Energy (Wh): convert to joules (value * 3600)
    """
    packet = emeterPacket(CONFIG['emeter']['serial_number'])
    packet.begin(int(time.time() * 1000), skip_phase_values=True)

    if total_negative_active_energy is None:
        total_negative_active_energy = energy / 1000  # Wh fallback converted to kWh

    # Add total values. Use dedicated total_negative_active_energy for SMA_NEGATIVE_ACTIVE_ENERGY.
    # total_negative_active_energy is in kWh; multiply by 1000 to get Wh, then by 3600 to get J.
    packet.addMeasurementValue(emeterPacket.SMA_POSITIVE_ACTIVE_POWER, 0)
    packet.addCounterValue(emeterPacket.SMA_POSITIVE_ACTIVE_ENERGY, 0)
    packet.addMeasurementValue(emeterPacket.SMA_NEGATIVE_ACTIVE_POWER, int(power * SCALING_FACTORS['power']))
    packet.addCounterValue(
        emeterPacket.SMA_NEGATIVE_ACTIVE_ENERGY,
        int(total_negative_active_energy * 1000 * SCALING_FACTORS['energy'])
    )

    # Add per-phase values
    add_phase_measurements(packet, '_L1', p1_power, p1_yield)
    add_phase_measurements(packet, '_L2', p2_power, p2_yield)
    add_phase_measurements(packet, '_L3', p3_power, p3_yield)

    packet.end()
    return bytes(packet.getData()[:packet.getLength()])


def _send_multicast(data: bytes, log_msg: str) -> None:
    """Send raw packet data via UDP multicast."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, CONFIG['multicast']['ttl'])
        multicast_config = CONFIG['multicast']
        sock.sendto(data, (multicast_config['address'], multicast_config['port']))
        logging.info(
            f"Sent {len(data)} bytes to {multicast_config['address']}:{multicast_config['port']}: "
            f"{log_msg}"
        )
    finally:
        sock.close()


def send_emeter_packet(power: int, energy: int, p1_power: int = 0, p1_yield: int = 0,
                       p2_power: int = 0, p2_yield: int = 0, p3_power: int = 0, p3_yield: int = 0,
                       total_negative_active_energy: Optional[float] = None,
                       log_prefix: str = '') -> None:
    """Build and send an emeterPacket with the given power/energy values."""
    data = build_emeter_packet(
        power, energy, p1_power, p1_yield, p2_power, p2_yield, p3_power, p3_yield,
        total_negative_active_energy
    )
    _send_multicast(data, (
        f"{log_prefix}"
        f"Total power: {power}W | Daily energy arg: {energy}Wh | "
        f"SMA_NEGATIVE_ACTIVE_ENERGY total: {total_negative_active_energy}kWh | "
        f"L1: {p1_power}W/{p1_yield}Wh | L2: {p2_power}W/{p2_yield}Wh | L3: {p3_power}W/{p3_yield}Wh"
    ))


def main() -> None:
    """Main function to start the data aggregator and virtual emeter"""
    try:
        # Validate configuration at startup
        validate_configuration()
    except ValueError as e:
        logging.error(f"Configuration error: {e}")
        return

    try:
        baseline_wh = load_total_yield_baseline_wh()
    except ValueError as e:
        logging.error(f"Configuration error: {e}")
        return

    total_counter_state = load_total_counter_state()
    try:
        # Ensure file exists from startup and is normalized.
        save_total_counter_state(total_counter_state)
    except Exception as e:
        logging.error(f"Unable to persist initial total counter state: {e}")
        return
    
    logging.info("Starting SMA data aggregator and virtual emeter...")

    # Values to keep constant while snooze is active.
    snooze_frozen_payload: Optional[Dict[str, Any]] = None
    snooze_was_active = False

    def build_fallback_snooze_payload() -> Dict[str, Any]:
        """Build a stable payload if startup happens during snooze."""
        fallback_daily_yield_wh = max(0, int(total_counter_state['day_max_wh']))
        p1_yield, p2_yield, p3_yield = distribute_phase_values(fallback_daily_yield_wh, 1, 1, 1)
        fallback_total_emitted_wh = (
            baseline_wh
            + total_counter_state['accumulated_wh']
            + fallback_daily_yield_wh
        )
        return {
            'energy': fallback_daily_yield_wh,
            'p1_yield': p1_yield,
            'p2_yield': p2_yield,
            'p3_yield': p3_yield,
            'total_negative_active_energy': fallback_total_emitted_wh / 1000
        }

    def build_payload_from_aggregate(agg: Dict[str, Any]) -> Dict[str, Any]:
        """Build emitted payload from aggregate data and persisted rollover state."""
        daily_yield_wh, total_emitted_wh = _update_rollover_state(
            total_counter_state, agg.get('daily_yield', 0), baseline_wh
        )
        return {
            'energy': daily_yield_wh,
            'p1_yield': agg.get('p1_yield', 0),
            'p2_yield': agg.get('p2_yield', 0),
            'p3_yield': agg.get('p3_yield', 0),
            'total_negative_active_energy': total_emitted_wh / 1000
        }
    
    # Define scheduled task that collects and sends data
    def scheduled_task() -> None:
        try:
            nonlocal total_counter_state
            nonlocal snooze_frozen_payload
            nonlocal snooze_was_active

            if is_snooze_time():
                if not snooze_was_active:
                    if snooze_frozen_payload is None:
                        # If we enter snooze without prior live data (e.g. startup during snooze),
                        # do one fresh read and freeze that snapshot for the whole snooze window.
                        try:
                            data_collection = collect_data()
                            if 'aggregate' in data_collection:
                                snooze_frozen_payload = build_payload_from_aggregate(data_collection['aggregate'])
                                logging.info("Snooze entry snapshot captured from fresh device read.")
                            else:
                                snooze_frozen_payload = build_fallback_snooze_payload()
                                logging.warning("Snooze entry snapshot unavailable from live data; using persisted fallback.")
                        except Exception as e:
                            snooze_frozen_payload = build_fallback_snooze_payload()
                            logging.warning(
                                f"Snooze entry fresh read failed ({e}); using persisted fallback snapshot."
                            )
                    logging.info("Snooze active: data reads paused, continuing packet transmission with frozen counters.")

                frozen = snooze_frozen_payload
                if frozen is None:
                    frozen = build_fallback_snooze_payload()

                send_emeter_packet(
                    power=0, p1_power=0, p2_power=0, p3_power=0,
                    log_prefix='SNOOZE ACTIVE - ', **frozen
                )
                snooze_was_active = True
                return

            if snooze_was_active:
                logging.info("Snooze ended: resuming live data reads.")
                snooze_was_active = False

            data_collection = collect_data()
            logging.debug(f"Data collection completed: {data_collection}")
            
            # Send the aggregated data as emeterPacket
            if 'aggregate' in data_collection:
                agg = data_collection['aggregate']
                payload = build_payload_from_aggregate(agg)

                send_emeter_packet(
                    power=agg.get('total_power', 0),
                    p1_power=agg.get('p1_power', 0),
                    p2_power=agg.get('p2_power', 0),
                    p3_power=agg.get('p3_power', 0),
                    **payload
                )

                snooze_frozen_payload = payload
        except Exception as e:
            logging.error(f"Error in scheduled task: {e}")
    
    # Schedule to run every N seconds (configurable)
    schedule.every(CONFIG['scheduler']['interval_seconds']).seconds.do(scheduled_task)

    # Run once at startup
    scheduled_task()

    # Main loop with improved error handling
    while True:
        try:
            schedule.run_pending()
            time.sleep(1)
        except KeyboardInterrupt:
            logging.info("Program terminated by user")
            break
        except Exception as e:
            logging.error(f"Error in main loop: {e}")
            time.sleep(60)  # Wait 1 minute on error

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logging.info("Program terminating...")
    finally:
        logging.info("Cleanup completed")