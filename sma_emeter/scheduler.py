import logging
import time
from typing import Any, Dict, Optional

from sma_emeter.aggregator import collect_data
from sma_emeter.device_state import DeviceCollectionState
from sma_emeter.phase_data import distribute_phase_values
from sma_emeter.config import CONFIG
from sma_emeter.emeter_packet import MulticastSender, send_emeter_packet
from sma_emeter.modbus_reader import ModbusClientPool
from sma_emeter.snooze import is_snooze_time
from sma_emeter.speedwire import SpeedwireCollector
from sma_emeter.state import (
    load_total_counter_state,
    load_total_yield_baseline_wh,
    save_total_counter_state,
    update_rollover_state,
)
from sma_emeter.types import EmeterPayload


class SchedulerState:
    """Manages mutable state for the data-collection loop."""

    def __init__(
        self,
        total_counter_state: Dict[str, Any],
        baseline_wh: int,
        modbus_pool: ModbusClientPool,
        speedwire: SpeedwireCollector,
        sender: MulticastSender,
    ) -> None:
        self.total_counter_state = total_counter_state
        self.baseline_wh = baseline_wh
        self.modbus_pool = modbus_pool
        self.speedwire = speedwire
        self.sender = sender
        self.device_state = DeviceCollectionState()
        self.snooze_frozen_payload: Optional[EmeterPayload] = None
        self.snooze_was_active = False

    def build_fallback_snooze_payload(self) -> EmeterPayload:
        fallback_daily_yield_wh = max(0, int(self.total_counter_state['day_max_wh']))
        p1_yield, p2_yield, p3_yield = distribute_phase_values(
            fallback_daily_yield_wh, 1, 1, 1
        )
        fallback_total_emitted_wh = (
            self.baseline_wh
            + self.total_counter_state['accumulated_wh']
            + fallback_daily_yield_wh
        )
        return EmeterPayload(
            power=0,
            energy=fallback_daily_yield_wh,
            p1_yield=p1_yield,
            p2_yield=p2_yield,
            p3_yield=p3_yield,
            total_negative_active_energy_kwh=fallback_total_emitted_wh / 1000,
        )

    def build_payload_from_aggregate(self, agg: Dict[str, Any]) -> EmeterPayload:
        daily_yield_wh, total_emitted_wh = update_rollover_state(
            self.total_counter_state, agg.get('daily_yield', 0), self.baseline_wh
        )
        return EmeterPayload(
            power=agg.get('total_power', 0),
            energy=daily_yield_wh,
            p1_power=agg.get('p1_power', 0),
            p2_power=agg.get('p2_power', 0),
            p3_power=agg.get('p3_power', 0),
            p1_yield=agg.get('p1_yield', 0),
            p2_yield=agg.get('p2_yield', 0),
            p3_yield=agg.get('p3_yield', 0),
            total_negative_active_energy_kwh=total_emitted_wh / 1000,
        )

    def _handle_snooze(self) -> None:
        if not self.snooze_was_active:
            if self.snooze_frozen_payload is None:
                try:
                    data_collection = collect_data(
                        self.modbus_pool, self.speedwire, self.device_state
                    )
                    if 'aggregate' in data_collection:
                        self.snooze_frozen_payload = self.build_payload_from_aggregate(
                            data_collection['aggregate']
                        )
                        logging.info("Snooze entry snapshot captured from fresh device read.")
                    else:
                        self.snooze_frozen_payload = self.build_fallback_snooze_payload()
                        logging.warning(
                            "Snooze entry snapshot unavailable from live data; using persisted fallback."
                        )
                except Exception:
                    self.snooze_frozen_payload = self.build_fallback_snooze_payload()
                    logging.warning(
                        "Snooze entry fresh read failed; using persisted fallback snapshot.",
                        exc_info=True,
                    )
            logging.info(
                "Snooze active: data reads paused, continuing packet transmission with frozen counters."
            )

        frozen = self.snooze_frozen_payload or self.build_fallback_snooze_payload()
        send_emeter_packet(
            self.sender,
            EmeterPayload(
                power=0,
                energy=frozen.energy,
                p1_yield=frozen.p1_yield,
                p2_yield=frozen.p2_yield,
                p3_yield=frozen.p3_yield,
                total_negative_active_energy_kwh=frozen.total_negative_active_energy_kwh,
                log_prefix='SNOOZE ACTIVE - ',
            ),
        )
        self.snooze_was_active = True

    def tick(self) -> None:
        try:
            if is_snooze_time():
                self._handle_snooze()
                return

            if self.snooze_was_active:
                logging.info("Snooze ended: resuming live data reads.")
                self.snooze_was_active = False

            data_collection = collect_data(
                self.modbus_pool, self.speedwire, self.device_state
            )
            logging.debug("Data collection completed: %s", data_collection)

            if 'aggregate' not in data_collection:
                return

            payload = self.build_payload_from_aggregate(data_collection['aggregate'])
            send_emeter_packet(self.sender, payload)
            self.snooze_frozen_payload = payload
        except Exception:
            logging.exception("Error in scheduled task")


def run_interval_loop(state: SchedulerState, interval_seconds: float) -> None:
    """Run collection ticks on a monotonic schedule without drift."""
    next_tick = time.monotonic()
    while True:
        try:
            state.tick()
        except KeyboardInterrupt:
            logging.info("Program terminated by user")
            break
        except Exception:
            logging.exception("Error in main loop")
            time.sleep(60)
            next_tick = time.monotonic()

        next_tick += interval_seconds
        sleep_for = next_tick - time.monotonic()
        if sleep_for > 0:
            time.sleep(sleep_for)
        else:
            logging.warning(
                "Tick overran interval by %.3fs; scheduling next tick immediately",
                -sleep_for,
            )
            next_tick = time.monotonic()


def main() -> None:
    try:
        baseline_wh = load_total_yield_baseline_wh()
    except ValueError as e:
        logging.error("Configuration error: %s", e)
        return

    total_counter_state = load_total_counter_state()
    try:
        save_total_counter_state(total_counter_state)
    except Exception:
        logging.exception("Unable to persist initial total counter state")
        return

    logging.info("Starting SMA data aggregator and virtual emeter...")

    modbus_pool = ModbusClientPool()
    speedwire = SpeedwireCollector()
    sender = MulticastSender()
    state = SchedulerState(total_counter_state, baseline_wh, modbus_pool, speedwire, sender)

    try:
        run_interval_loop(state, CONFIG['scheduler']['interval_seconds'])
    finally:
        modbus_pool.close_all()
        speedwire.close()
        sender.close()
        logging.info("Cleanup completed")
