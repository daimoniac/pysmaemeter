from dataclasses import dataclass
from typing import Optional


@dataclass
class EmeterPayload:
    """Values passed to emeter packet build/send."""

    power: int
    energy: int  # daily yield (Wh)
    total_negative_active_energy_kwh: Optional[float] = None
    p1_power: int = 0
    p1_yield: int = 0
    p2_power: int = 0
    p2_yield: int = 0
    p3_power: int = 0
    p3_yield: int = 0
    log_prefix: str = ''

    def lifetime_energy_wh(self) -> int:
        """Lifetime negative active energy in Wh for packet encoding."""
        if self.total_negative_active_energy_kwh is None:
            return self.energy
        return int(self.total_negative_active_energy_kwh * 1000)
