import socket
import time
from emeter import emeterPacket

# Multicast address and port (SMA default)
UDP_ADDRESS = '239.12.255.254'
UDP_PORT = 9522

def main():
    # serial number for the virtual emeter device
    serial_number = 12345678

    # Create a new emeter packet with the given serial number
    packet = emeterPacket(serial_number)

    # Start the packet with the current timestamp in milliseconds
    packet.begin(int(time.time() * 1000))

    # Add a measurement value (e.g., positive active power in watts)
    packet.addMeasurementValue(emeterPacket.SMA_POSITIVE_ACTIVE_POWER, 1234)

    # Add a counter value (e.g., positive active energy in watt-hours)
    packet.addCounterValue(emeterPacket.SMA_POSITIVE_ACTIVE_ENERGY, 567890)

    # Finalize the packet
    packet.end()

    # Retrieve the raw packet data
    data = packet.getData()[:packet.getLength()]

    # Create a UDP socket for sending the packet
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)

    # Set the time-to-live for multicast packets to 32 hops
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 32)

    # Send the packet data to the multicast address and port
    sock.sendto(data, (UDP_ADDRESS, UDP_PORT))
    print(f"Sent {len(data)} bytes to {UDP_ADDRESS}:{UDP_PORT}")

if __name__ == "__main__":
    main()