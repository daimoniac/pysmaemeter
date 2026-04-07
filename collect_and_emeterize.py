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
    and returns a list of values. Uses persistent ModbusTcpClient with retry logic.
    
    Args:
        ip_addr: Last octet of IP address (e.g., '191' for 192.168.10.191)
        sma_class: Device type/class
        retries: Number of connection retry attempts (uses config value if None)
    
    Returns:
        List of register values or None if all attempts failed
    """
    modbus_config = CONFIG['modbus']
    if retries is None:
        retries = modbus_config['retries']
    
    local_values: List[int] = []
    unit_id = modbus_config['unit_id']
    host = f"{modbus_config['base_ip']}.{ip_addr}"
    
    for attempt in range(retries):
        client = None
        try:
            # Create a new connection for each attempt
            client = ModbusTcpClient(host, port=modbus_config['port'])
            if not client.connect():
                # Connection error - may be transient
                raise ConnectionError(f"Connection to {host}:{modbus_config['port']} not possible")
            
            # Read all registers for this device
            failed_registers = []
            for addr in REGISTERS[sma_class]:
                try:
                    result = client.read_holding_registers(address=addr, count=2, unit=unit_id)
                    # Check if result is an exception response
                    if hasattr(result, 'isError') and result.isError():
                        logging.warning(f"Skipping invalid register {addr} from {host}: {result}")
                        failed_registers.append(addr)
                        continue
                    if not hasattr(result, 'registers'):
                        logging.warning(f"Skipping register {addr} from {host}: Invalid response {result}")
                        failed_registers.append(addr)
                        continue
                    logging.debug(f"Reading register {addr} from {host} result: {result.registers[1]}")
                    local_values.append(result.registers[1])
                except Exception as e:
                    # Register read failed - skip this register and continue
                    logging.warning(f"Skipping register {addr} from {host}: {e}")
                    failed_registers.append(addr)
                    continue
            
            # Check if we got any data at all
            if not local_values:
                raise Exception(f"No valid registers readable from {host} (failed: {failed_registers})")

            # All reads successful
            logging.debug(f"Successfully read {len(local_values)} registers from {host}")
            return local_values
            
        except ConnectionError as e:
            # Transient connection error - retry
            logging.warning(f"Attempt {attempt + 1}/{retries} - Connection failed for {host}: {e}")
            if attempt < retries - 1:
                time.sleep(modbus_config['retry_delay'])  # Wait before retry
            continue
            
        except Exception as e:
            # Other error - don't retry
            logging.error(f"Permanent error reading from {host}: {e}")
            return None
            
        finally:
            if client:
                client.close()
    
    logging.error(f"Failed to read from {host} after {retries} attempts")
    return None


def collect_speedwire_data_sync() -> Dict[str, int]:
    """Synchronous wrapper function for Speedwire data collection"""
    try:
        return asyncio.run(get_speedwire_data())
    except Exception as e:
        logging.error(f"Error collecting Speedwire data: {e}")
        return {'spotacpower': 0, 'tagesertrag': 0}


def _extract_phase_data(total_power: int, daily_yield: int, phase_power_data: Dict[str, int]) -> Dict[str, int]:
    """Extract and calculate phase data from raw device data"""
    # Get phase powers (with fallback to equal distribution)
    p1_power = phase_power_data.get('p1_power', 0) or total_power // 3
    p2_power = phase_power_data.get('p2_power', 0) or total_power // 3
    p3_power = phase_power_data.get('p3_power', 0) or (total_power - p1_power - p2_power)
    
    # Get phase yields (calculate if not provided)
    if all(phase_power_data.get(f'p{i}_yield') for i in [1, 2, 3]):
        p1_yield = phase_power_data['p1_yield']
        p2_yield = phase_power_data['p2_yield']
        p3_yield = phase_power_data['p3_yield']
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
            if device_info['type'] == 8001:
                # Inverter data via Modbus
                data = get_values_persistent(device_id, device_info['type'])
                if data is None:
                    logging.warning(f"No data from device {device_id} ({device_info['name']})")
                    continue
                
                # Extract values from registers
                # Registers: [30773, 30961, 30775, 30535, 30777, 30779, 30781]
                total_power = data[2]     # 30775: Total AC power
                daily_yield = data[3]     # 30535: Daily yield in Wh
                
                # Get phase data from registers
                phase_data = _extract_phase_data(total_power, daily_yield, {
                    'p1_power': data[4],  # 30777: Power L1
                    'p2_power': data[5],  # 30779: Power L2
                    'p3_power': data[6]   # 30781: Power L3
                })
                
                data_collection[device_id] = {
                    'total_power': total_power,
                    'daily_yield': daily_yield,
                    **phase_data
                }

            elif device_info['type'] == 9999:
                # Speedwire data
                data = collect_speedwire_data_sync()
                total_power = data.get('spotacpower', 0)
                daily_yield = data.get('tagesertrag', 0)
                
                # Get phase data from speedwire
                phase_data = _extract_phase_data(total_power, daily_yield, data)
                
                data_collection[device_id] = {
                    'total_power': total_power,
                    'daily_yield': daily_yield,
                    **phase_data
                }

        except Exception as e:
            logging.error(f"Error collecting data for device {device_id}: {e}")
            continue

    # Calculate aggregate totals per phase
    aggregate = {
        'p1_power': sum(d.get('p1_power', 0) for d in data_collection.values()),
        'p1_yield': sum(d.get('p1_yield', 0) for d in data_collection.values()),
        'p2_power': sum(d.get('p2_power', 0) for d in data_collection.values()),
        'p2_yield': sum(d.get('p2_yield', 0) for d in data_collection.values()),
        'p3_power': sum(d.get('p3_power', 0) for d in data_collection.values()),
        'p3_yield': sum(d.get('p3_yield', 0) for d in data_collection.values())
    }
    
    # Total power and yield are sum of phases
    aggregate['total_power'] = aggregate['p1_power'] + aggregate['p2_power'] + aggregate['p3_power']
    aggregate['daily_yield'] = aggregate['p1_yield'] + aggregate['p2_yield'] + aggregate['p3_yield']
    
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


def send_emeter_packet(power: int, energy: int, p1_power: int = 0, p1_yield: int = 0,
                       p2_power: int = 0, p2_yield: int = 0, p3_power: int = 0, p3_yield: int = 0,
                       total_negative_active_energy: Optional[float] = None) -> None:
    """Sends an emeterPacket with the given power (W) and energy (Wh) including per-phase data.
    
    SMA convention: 
    - Positive (consume) = power drawn from grid
    - Negative (supply) = power fed into grid
    For PV inverters, we're supplying power, so we use negative values.
    
    SMA uses scaling factors defined in SCALING_FACTORS constant:
    - Power (W): scale factor {SCALING_FACTORS['power']} (store value * {SCALING_FACTORS['power']})
    - Energy (Wh): convert to joules (value * {SCALING_FACTORS['energy']})
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

    # Retrieve the raw packet data
    data = packet.getData()[:packet.getLength()]

    # Create and send UDP multicast packet
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, CONFIG['multicast']['ttl'])
        multicast_config = CONFIG['multicast']
        sock.sendto(data, (multicast_config['address'], multicast_config['port']))
        logging.info(
            f"Sent {len(data)} bytes to {multicast_config['address']}:{multicast_config['port']}: "
            f"Total power: {power}W | Daily energy arg: {energy}Wh | "
            f"SMA_NEGATIVE_ACTIVE_ENERGY total: {total_negative_active_energy}kWh | "
            f"L1: {p1_power}W/{p1_yield}Wh | L2: {p2_power}W/{p2_yield}Wh | L3: {p3_power}W/{p3_yield}Wh"
        )
    finally:
        sock.close()


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
    
    # Define scheduled task that collects and sends data
    def scheduled_task() -> None:
        try:
            nonlocal total_counter_state

            data_collection = collect_data()
            logging.debug(f"Data collection completed: {data_collection}")
            
            # Send the aggregated data as emeterPacket
            if 'aggregate' in data_collection:
                agg = data_collection['aggregate']
                current_date = _current_date()
                current_daily_yield_wh = max(0, int(agg.get('daily_yield', 0) or 0))
                state_changed = False

                # Midnight rollover: add previous day max once and start a fresh day.
                # On the rollover cycle, discard the current daily yield reading
                # because inverters may not have reset their daily counters yet,
                # causing a stale reading to be captured as the new day's max.
                if total_counter_state['date'] != current_date:
                    previous_day = total_counter_state['date']
                    previous_day_max_wh = total_counter_state['day_max_wh']
                    total_counter_state['accumulated_wh'] += previous_day_max_wh
                    total_counter_state['date'] = current_date
                    total_counter_state['day_max_wh'] = 0
                    state_changed = True
                    current_daily_yield_wh = 0
                    logging.info(
                        f"Daily rollover {previous_day} -> {current_date}: "
                        f"added {previous_day_max_wh}Wh to accumulated total "
                        f"({total_counter_state['accumulated_wh']}Wh)"
                    )
                elif current_daily_yield_wh > total_counter_state['day_max_wh']:
                    total_counter_state['day_max_wh'] = current_daily_yield_wh
                    state_changed = True

                if state_changed:
                    save_total_counter_state(total_counter_state)

                # Use the higher of current daily yield or day_max to prevent
                # the total counter going backwards when inverters reset their
                # daily counters at local midnight (before our server rollover).
                effective_daily_yield_wh = max(
                    current_daily_yield_wh,
                    total_counter_state['day_max_wh']
                )
                total_emitted_yield_wh = (
                    baseline_wh
                    + total_counter_state['accumulated_wh']
                    + effective_daily_yield_wh
                )
                logging.debug(
                    f"Total emitted yield: baseline={baseline_wh}Wh, "
                    f"accumulated={total_counter_state['accumulated_wh']}Wh, "
                    f"today={current_daily_yield_wh}Wh, effective={effective_daily_yield_wh}Wh, "
                    f"emitted={total_emitted_yield_wh}Wh"
                )

                send_emeter_packet(
                    power=agg.get('total_power', 0),
                    energy=current_daily_yield_wh,
                    p1_power=agg.get('p1_power', 0),
                    p1_yield=agg.get('p1_yield', 0),
                    p2_power=agg.get('p2_power', 0),
                    p2_yield=agg.get('p2_yield', 0),
                    p3_power=agg.get('p3_power', 0),
                    p3_yield=agg.get('p3_yield', 0),
                    total_negative_active_energy=total_emitted_yield_wh / 1000
                )
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