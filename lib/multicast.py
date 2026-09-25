"""UDP multicast sender for SMA Energy Meter packets."""

import socket


def send_udp_multicast(data: bytes, address: str, port: int, ttl: int = 32) -> None:
    """Send a datagram to a UDP multicast group."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, int(ttl))
        sock.sendto(data, (address, int(port)))
    finally:
        sock.close()
