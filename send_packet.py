import socket
import time
from emeter import emeterPacket  # <-- Use relative import

# Multicast address and port (as used in config.py)
UDP_ADDRESS = '239.12.255.254'
UDP_PORT = 9522

def main():
    # Example serial number
    serial_number = 12345678
    packet = emeterPacket(serial_number)
    packet.begin(int(time.time() * 1000))
    # Add some example values
    packet.addMeasurementValue(emeterPacket.SMA_POSITIVE_ACTIVE_POWER, 1234)
    packet.addCounterValue(emeterPacket.SMA_POSITIVE_ACTIVE_ENERGY, 567890)
    packet.end()

    data = packet.getData()[:packet.getLength()]

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 32)
    sock.sendto(data, (UDP_ADDRESS, UDP_PORT))
    print(f"Sent {len(data)} bytes to {UDP_ADDRESS}:{UDP_PORT}")

if __name__ == "__main__":
    main()