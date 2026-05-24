#!/usr/bin/python
import asyncio
import codecs
import struct
from struct import unpack

# Konfiguration
user_pw = '271257Fs'
code_login = 0xfffd040d
code_total_today = 0x54000201
code_spot_ac_power = 0x51000201
code_spot_ac_power_l1 = 0x51000215  # Phase 1 power
code_spot_ac_power_l2 = 0x51000229  # Phase 2 power
code_spot_ac_power_l3 = 0x5100023D  # Phase 3 power
src_serial = 1915013455
dst_serial = 4294967295
comm_port = 9522
comm_dst = '192.168.10.192'

cmd_login = '534d4100000402a000000001003a001060650ea0ffffffffffff000178004fc9247200010000000004800c04fdff07000000840300004c20cb5100000000babfb9babdbfcefb8888888800000000'
cmd_query_total_today = '534d4100000402a00000000100260010606509a0ffffffffffff00007800%s000000000000f180000200540001260022ff260000000000' % (codecs.decode(codecs.encode((struct.pack('<I', src_serial)),'hex')))
cmd_query_spot_ac_power = '534d4100000402a00000000100260010606509e0ffffffffffff00007800%s00000000000081f0000200510001260022ff260000000000' % (codecs.decode(codecs.encode((struct.pack('<I', src_serial)),'hex')))

class SpeedwireClient:
    def __init__(self, loop):
        self.loop = loop
        self.transport = None
        self.response_future = self.loop.create_future()
        self.sma_data = {}

    async def send_command(self, cmd):
        data = codecs.decode(cmd, 'hex')
        self.transport.sendto(data, (comm_dst, comm_port))

    def connection_made(self, transport):
        self.transport = transport
        self.loop.create_task(self.send_command(cmd_login))

    def datagram_received(self, data, addr):
        code = self.get_code(data)

        if code == code_login:
            self.loop.create_task(self.send_command(cmd_query_total_today))

        if code == code_total_today:
            self.sma_data['tagesertrag'] = self.get_long_value_at(data, 78)
            self.loop.create_task(self.send_command(cmd_query_spot_ac_power))

        if code == code_spot_ac_power:
            # Extract total power and phase-specific power
            values = [self.get_long_value_at(data, i) for i in range(62, len(data), 4) if self.get_long_value_at(data, i) != 0x80000000]
            self.sma_data['spotacpower'] = max(values) if values else 0
            
            # Try to extract phase-specific data (positions may vary)
            # For now, distribute evenly - will need actual multicast data to find real positions
            total_power = self.sma_data['spotacpower']
            self.sma_data['p1_power'] = total_power // 3
            self.sma_data['p2_power'] = total_power // 3
            self.sma_data['p3_power'] = total_power - self.sma_data['p1_power'] - self.sma_data['p2_power']
            
            # Same for daily yield
            total_yield = self.sma_data.get('tagesertrag', 0)
            self.sma_data['p1_yield'] = total_yield // 3
            self.sma_data['p2_yield'] = total_yield // 3
            self.sma_data['p3_yield'] = total_yield - self.sma_data['p1_yield'] - self.sma_data['p2_yield']
            
            self.response_future.set_result(self.sma_data)

    def connection_lost(self, exc):
        """Called when the connection is lost or closed."""
        if exc:
            # If there was an error, log it
            import logging
            logging.warning(f"Speedwire connection lost: {exc}")
        # If the response future hasn't been resolved yet, set it with the data we have
        if not self.response_future.done():
            self.response_future.set_result(self.sma_data)

    def get_code(self, data):
        return unpack('I', data[42:46])[0]

    def get_long_value_at(self, data, index):
        return unpack('I', data[index:index + 4])[0]

    async def get_speedwire_data(self, timeout: float = 2.0):
        transport, _protocol = await self.loop.create_datagram_endpoint(
            lambda: self,
            local_addr=("0.0.0.0", 0)
        )
        try:
            await asyncio.wait_for(self.response_future, timeout=timeout)
            return self.sma_data
        finally:
            transport.close()

async def fetch_speedwire_data(timeout: float = 2.0):
    loop = asyncio.get_event_loop()
    client = SpeedwireClient(loop)
    return await client.get_speedwire_data(timeout=timeout)