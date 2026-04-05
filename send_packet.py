import socket
import time
import argparse
from lib.emeter import emeterPacket

# Multicast address and port (SMA default)
DEFAULT_UDP_ADDRESS: str = '239.12.255.254'
DEFAULT_UDP_PORT: int = 9522
DEFAULT_SERIAL_NUMBER: int = 12345678
DEFAULT_ACTIVE_POWER: int = 1234
DEFAULT_ACTIVE_ENERGY: int = 567890
DEFAULT_TTL: int = 32

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Send a virtual emeter packet via UDP multicast."
    )
    parser.add_argument(
        '--serial', type=int, default=DEFAULT_SERIAL_NUMBER,
        help='Serial number for the emeter device'
    )
    parser.add_argument(
        '--address', type=str, default=DEFAULT_UDP_ADDRESS,
        help='UDP multicast address'
    )
    parser.add_argument(
        '--port', type=int, default=DEFAULT_UDP_PORT,
        help='UDP port'
    )
    parser.add_argument(
        '--power', type=int, default=DEFAULT_ACTIVE_POWER,
        help='Positive active power in watts'
    )
    parser.add_argument(
        '--energy', type=int, default=DEFAULT_ACTIVE_ENERGY,
        help='Positive active energy in watt-hours'
    )

    args = parser.parse_args()

    # Create a new emeter packet with the given serial number
    packet = emeterPacket(args.serial)

    # Start the packet with the current timestamp in milliseconds
    packet.begin(int(time.time() * 1000))

    # Add a measurement value (e.g., positive active power in watts)
    packet.addMeasurementValue(
        emeterPacket.SMA_POSITIVE_ACTIVE_POWER, args.power
    )

    # Add a counter value (e.g., positive active energy in watt-hours)
    # Note: The energy value is multiplied by 3600 to convert from watt-hours to joules
    packet.addCounterValue(
        emeterPacket.SMA_POSITIVE_ACTIVE_ENERGY, args.energy*3600
    )

    # Finalize the packet
    packet.end()

    # Retrieve the raw packet data
    data = packet.getData()[:packet.getLength()]

    # Create a UDP socket for sending the packet
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)

    # Set the time-to-live for multicast packets
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, DEFAULT_TTL)

    # Send the packet data to the multicast address and port
    sock.sendto(data, (args.address, args.port))
    print(f"Sent {len(data)} bytes to {args.address}:{args.port}")


if __name__ == "__main__":
    main()