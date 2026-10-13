"""QUIC bulk-sender (aioquic). Same request/trailer protocol as server.py,
over one bidirectional stream: "GET quic <nbytes>\n" -> payload + 128B JSON trailer.

aioquic implements reno and cubic only (no BBR), so QUIC here runs cubic.
Loss/RTT come from hooks into aioquic's recovery object (private attributes,
so they may change between aioquic versions; tested on 1.3.0).
"""
import asyncio
import json
import os
import struct
import time

from aioquic.asyncio import QuicConnectionProtocol, serve
from aioquic.quic.configuration import QuicConfiguration
from aioquic.quic.events import StreamDataReceived

PORT = int(os.environ.get("QUIC_PORT", 4433))
ALPN = "bench"
TRAILER = 128
WINDOW = 64 * 1024 * 1024


class BenchServer(QuicConnectionProtocol):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.sent = 0
        self.lost = 0
        self._bufs = {}
        loss = self._quic._loss

        orig_sent, orig_lost = loss.on_packet_sent, loss._on_packets_lost

        def on_sent(*args, **kwargs):
            self.sent += 1
            return orig_sent(*args, **kwargs)

        def on_lost(*, now, packets, space):
            packets = list(packets)
            self.lost += len(packets)
            return orig_lost(now=now, packets=packets, space=space)

        loss.on_packet_sent = on_sent
        loss._on_packets_lost = on_lost

    def quic_event_received(self, event):
        if not isinstance(event, StreamDataReceived):
            return
        buf = self._bufs.get(event.stream_id, b"") + event.data
        self._bufs[event.stream_id] = buf
        if b"\n" in buf:
            self._bufs[event.stream_id] = b""
            parts = buf.split(b"\n")[0].decode().split()
            if len(parts) == 3 and parts[0] == "GET":
                asyncio.ensure_future(self._serve(event.stream_id, int(parts[2])))
            elif len(parts) == 6 and parts[0] == "MUXMSG":
                # MUXMSG <n_total> <stream_index> <msgs> <size> <interval_ms>
                asyncio.ensure_future(self._mux(
                    event.stream_id, int(parts[1]), int(parts[2]), int(parts[3]),
                    int(parts[4]), float(parts[5]) / 1000.0))

    async def _mux(self, sid, n_total, idx, msgs, size, interval):
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        for k in range(msgs):
            delay = t0 + k * interval + idx * interval / n_total - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            payload = struct.pack("!d", time.time()).ljust(size, b"x")
            self._quic.send_stream_data(sid, payload, end_stream=(k == msgs - 1))
            self.transmit()

    async def _serve(self, sid, nbytes):
        self._quic.send_stream_data(sid, b"x" * nbytes, end_stream=False)
        self.transmit()
        sender = self._quic._streams[sid].sender
        loss = self._quic._loss
        # wait until everything is sent and acked, so loss counts are complete
        while not (sender.buffer_is_empty and loss.bytes_in_flight == 0):
            await asyncio.sleep(0.005)
        stats = {
            "rtt_ms": loss._rtt_smoothed * 1000.0,
            "retrans": self.lost,
            "loss": self.lost / max(1, self.sent),
            "cc_applied": "quic",
        }
        self._quic.send_stream_data(
            sid, json.dumps(stats).encode().ljust(TRAILER, b" "), end_stream=True)
        self.transmit()


async def main():
    cfg = QuicConfiguration(
        is_client=False,
        alpn_protocols=[ALPN],
        congestion_control_algorithm="cubic",
        max_data=WINDOW,
        max_stream_data=WINDOW,
    )
    cfg.load_cert_chain(os.environ.get("CERT", "cert.pem"), os.environ.get("KEY", "key.pem"))
    await serve("0.0.0.0", PORT, configuration=cfg, create_protocol=BenchServer)
    print(f"quic listening on udp/{PORT}", flush=True)
    await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
