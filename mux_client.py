"""Multi-stream head-of-line-blocking test.

N logical streams each emit one small message every INTERVAL ms (evenly staggered).
We measure per-message latency (receiver clock - sender timestamp; same-host
containers share a clock) under three transports:

  tcp-mux      all N streams multiplexed on ONE TCP connection (HTTP/2-style)
  tcp-parallel N separate TCP connections, one stream each (HTTP/1.1-style)
  quic         N streams on ONE QUIC connection

A lost packet stalls everything behind it on a TCP connection (HOL blocking),
but only the affected stream on QUIC.
"""
import argparse
import asyncio
import csv
import os
import socket
import struct
import threading
import time

from client import close_quic, open_quic

HDR = struct.Struct("!II")
TS = struct.Struct("!d")


def pct(values, p):
    s = sorted(values)
    return s[min(len(s) - 1, int(round(p / 100 * (len(s) - 1))))]


def run_tcp(host, port, ids, n_total, msgs, size, interval_ms, out):
    s = socket.create_connection((host, port))
    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    s.sendall(f"MUXMSG {n_total} {','.join(map(str, ids))} {msgs} {size} {interval_ms}\n".encode())
    expected, buf = msgs * len(ids), bytearray()
    lats = []
    while len(lats) < expected:
        data = s.recv(65536)
        if not data:
            break
        now = time.time()
        buf += data
        while len(buf) >= HDR.size:
            _, ln = HDR.unpack_from(buf, 0)
            if len(buf) < HDR.size + ln:
                break
            lats.append((now - TS.unpack_from(buf, HDR.size)[0]) * 1000)
            del buf[:HDR.size + ln]
    s.close()
    out.extend(lats)


def tcp_mux(host, port, n, msgs, size, interval_ms):
    out = []
    run_tcp(host, port, list(range(n)), n, msgs, size, interval_ms, out)
    return out


def tcp_parallel(host, port, n, msgs, size, interval_ms):
    outs = [[] for _ in range(n)]
    threads = [threading.Thread(target=run_tcp,
                                args=(host, port, [i], n, msgs, size, interval_ms, outs[i]))
               for i in range(n)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    return [x for o in outs for x in o]


async def _quic(host, port, n, msgs, size, interval_ms):
    from aioquic.asyncio import QuicConnectionProtocol
    from aioquic.quic.events import ConnectionTerminated, StreamDataReceived

    class Proto(QuicConnectionProtocol):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.bufs, self.lats, self.ended = {}, [], 0
            self.done = asyncio.get_running_loop().create_future()

        def quic_event_received(self, ev):
            if isinstance(ev, StreamDataReceived):
                now = time.time()
                b = self.bufs.setdefault(ev.stream_id, bytearray())
                b += ev.data
                while len(b) >= size:
                    self.lats.append((now - TS.unpack_from(b, 0)[0]) * 1000)
                    del b[:size]
                if ev.end_stream:
                    self.ended += 1
                    if self.ended == n and not self.done.done():
                        self.done.set_result(None)
            elif isinstance(ev, ConnectionTerminated) and not self.done.done():
                self.done.set_exception(ConnectionError("quic connection closed"))

    transport, p = await open_quic(host, port, Proto)
    try:
        for i in range(n):
            sid = p._quic.get_next_available_stream_id()
            p._quic.send_stream_data(
                sid, f"MUXMSG {n} {i} {msgs} {size} {interval_ms}\n".encode(), end_stream=False)
        p.transmit()
        await asyncio.wait_for(p.done, msgs * interval_ms / 1000 + 30)
        return p.lats
    finally:
        if p.done.done() and not p.done.cancelled():
            p.done.exception()
        await close_quic(transport, p)


def quic(host, port, n, msgs, size, interval_ms):
    return asyncio.run(_quic(host, port, n, msgs, size, interval_ms))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="server")
    ap.add_argument("--port", type=int, default=5001)
    ap.add_argument("--quic-port", type=int, default=4433)
    ap.add_argument("--mode", required=True, choices=["tcp-mux", "tcp-parallel", "quic"])
    ap.add_argument("--streams", type=int, default=8)
    ap.add_argument("--msgs", type=int, default=100, help="messages per stream")
    ap.add_argument("--size", type=int, default=1200, help="bytes per message (>= 8)")
    ap.add_argument("--interval-ms", type=int, default=20)
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--profile", default="unlabeled")
    ap.add_argument("--csv", default="results/mux.csv")
    a = ap.parse_args()

    os.makedirs(os.path.dirname(a.csv) or ".", exist_ok=True)
    new_file = not os.path.exists(a.csv)
    with open(a.csv, "a", newline="") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["profile", "mode", "run", "messages", "p50_ms", "p95_ms", "p99_ms", "max_ms"])
        for i in range(a.runs):
            if a.mode == "quic":
                lats = quic(a.host, a.quic_port, a.streams, a.msgs, a.size, a.interval_ms)
            elif a.mode == "tcp-mux":
                lats = tcp_mux(a.host, a.port, a.streams, a.msgs, a.size, a.interval_ms)
            else:
                lats = tcp_parallel(a.host, a.port, a.streams, a.msgs, a.size, a.interval_ms)
            expected = a.streams * a.msgs
            if len(lats) < expected:
                print(f"WARNING: got {len(lats)}/{expected} messages")
            row = [a.profile, a.mode, i, len(lats)] + [f"{pct(lats, p):.1f}" for p in (50, 95, 99, 100)]
            w.writerow(row)
            print(f"{a.profile:10} {a.mode:12} run={i} n={len(lats)} "
                  f"p50={row[4]} p95={row[5]} p99={row[6]} max={row[7]} ms", flush=True)


if __name__ == "__main__":
    main()
