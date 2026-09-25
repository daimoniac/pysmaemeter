"""SMA Energy Meter wire scaling."""

# Instantaneous power is stored as value * 10 (0.1 W resolution).
POWER_SCALE = 10
# Energy counters are watt-hours converted to joules.
ENERGY_WH_TO_JOULE = 3600
