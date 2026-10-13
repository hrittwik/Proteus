"""Client with fixed and adaptive congestion-control strategies."""
import argparse
import asyncio
import csv
import json
import os
import socket
import ssl
import time

TRAILER = 128


def fetch_tcp(host, port, cc, nbytes):
    t0 = time.perf_counter()
    s = socket.create_connection((host, port))
    s.sendall(f"GET {cc} {nbytes}\n".encode())
    total = nbytes + TRAILER
    buf = bytearray(total)
    view, got = memoryview(buf), 0
    while got < total:
        n = s.recv_into(view[got:], min(65536, total - got))
        if n == 0:
            raise ConnectionError("server closed early")
        got += n
    elapsed = time.perf_counter() - t0
    s.close()
    stats = json.loads(bytes(buf[nbytes:]).decode().strip())
    stats["mbps"] = nbytes * 8 / elapsed / 1e6
    stats["cc_requested"] = cc
    return stats


QUIC_TIMEOUT = float(os.environ.get("QUIC_TIMEOUT", 5))
QUIC_WINDOW = 64 * 1024 * 1024


async def open_quic(host, port, proto_cls):
    """Connect over IPv4 (aioquic.connect() hard-codes IPv6 dual-stack, which fails on
    hosts/containers without IPv6). Returns (transport, protocol); call close_quic()."""
    from aioquic.quic.configuration import QuicConfiguration
    from aioquic.quic.connection import QuicConnection

    cfg = QuicConfiguration(
        is_client=True, alpn_protocols=["bench"], verify_mode=ssl.CERT_NONE,
        congestion_control_algorithm="cubic",
        max_data=QUIC_WINDOW, max_stream_data=QUIC_WINDOW)
    cfg.server_name = host
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, family=socket.AF_INET, type=socket.SOCK_DGRAM)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", 0))
    transport, p = await loop.create_datagram_endpoint(
        lambda: proto_cls(QuicConnection(configuration=cfg)), sock=sock)
    try:
        p.connect(infos[0][4], transmit=True)
        await p.wait_connected()
    except BaseException:
        transport.close()
        raise
    return transport, p


async def close_quic(transport, p):
    p.close()
    await p.wait_closed()
    transport.close()


async def _fetch_quic(host, port, nbytes):
    from aioquic.asyncio import QuicConnectionProtocol
    from aioquic.quic.events import ConnectionTerminated, StreamDataReceived

    class Proto(QuicConnectionProtocol):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.buf = bytearray()
            self.done = asyncio.get_running_loop().create_future()

        def quic_event_received(self, event):
            if isinstance(event, StreamDataReceived):
                self.buf += event.data
                if event.end_stream and not self.done.done():
                    self.done.set_result(None)
            elif isinstance(event, ConnectionTerminated) and not self.done.done():
                self.done.set_exception(ConnectionError("quic connection closed"))

    transport, p = await open_quic(host, port, Proto)
    try:
        sid = p._quic.get_next_available_stream_id()
        p._quic.send_stream_data(sid, f"GET quic {nbytes}\n".encode(), end_stream=False)
        p.transmit()
        await p.done
        return json.loads(bytes(p.buf[nbytes:nbytes + TRAILER]).decode().strip())
    finally:
        if p.done.done() and not p.done.cancelled():
            p.done.exception()  # mark retrieved; avoids asyncio "never retrieved" noise
        await close_quic(transport, p)


def fetch_quic(host, port, nbytes):
    t0 = time.perf_counter()
    stats = asyncio.run(asyncio.wait_for(_fetch_quic(host, port, nbytes), QUIC_TIMEOUT))
    stats["mbps"] = nbytes * 8 / (time.perf_counter() - t0) / 1e6
    stats["cc_requested"] = "quic"
    return stats


def fetch(host, tcp_port, quic_port, choice, nbytes):
    if choice == "quic":
        return fetch_quic(host, quic_port, nbytes)
    return fetch_tcp(host, tcp_port, choice, nbytes)


def connect_rtt(host, port, n=3):
    """Base path RTT in ms: min TCP handshake time over n connects (no queueing, arm-independent)."""
    best = float("inf")
    for _ in range(n):
        t0 = time.perf_counter()
        socket.create_connection((host, port), timeout=5).close()
        best = min(best, (time.perf_counter() - t0) * 1000)
    return best


class Fixed:
    def __init__(self, cc):
        self.cc = cc

    def next_cc(self):
        return self.cc

    def observe(self, result):
        pass


class Adaptive:
    """EWMA of loss/RTT -> pick cubic or bbr, with hysteresis (patience)."""

    def __init__(self, loss_thr=0.005, rtt_thr_ms=80.0, patience=2, alpha=0.5):
        self.cc, self.loss, self.rtt = "cubic", None, None
        self.loss_thr, self.rtt_thr, self.patience, self.alpha = loss_thr, rtt_thr_ms, patience, alpha
        self.pending, self.count = None, 0

    def next_cc(self):
        return self.cc

    def _ewma(self, old, new):
        return new if old is None else self.alpha * new + (1 - self.alpha) * old

    def observe(self, r):
        self.loss = self._ewma(self.loss, r["loss"])
        self.rtt = self._ewma(self.rtt, r["rtt_ms"])
        want = "bbr" if (self.loss > self.loss_thr or self.rtt > self.rtt_thr) else "cubic"
        if want == self.cc:
            self.pending, self.count = None, 0
        elif want == self.pending:
            self.count += 1
            if self.count >= self.patience:
                self.cc, self.pending, self.count = want, None, 0
        else:
            self.pending, self.count = want, 1


class QuicFirst:
    """Try QUIC; if it fails (UDP blocked, timeout), fall back to TCP for good."""

    def __init__(self, fallback="cubic"):
        self.fallback, self.quic_ok = fallback, True

    def next_cc(self):
        return "quic" if self.quic_ok else self.fallback

    def observe(self, result):
        pass

    def on_failure(self):
        self.quic_ok = False


def make_strategy(name, a):
    if name == "adaptive":
        return Adaptive()
    if name == "quic-first":
        return QuicFirst()
    if name == "learned":
        from learned import Learned
        return Learned(a.policy, a.train, a.alpha, lambda: connect_rtt(a.host, a.port), a.epsilon)
    return Fixed(name)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="server")
    ap.add_argument("--port", type=int, default=5001, help="TCP port")
    ap.add_argument("--quic-port", type=int, default=4433)
    ap.add_argument("--strategy", default="adaptive", help="cubic | bbr | reno | quic | adaptive | quic-first | learned")
    ap.add_argument("--runs", type=int, default=8)
    ap.add_argument("--size-mb", type=float, default=5)
    ap.add_argument("--profile", default="unlabeled")
    ap.add_argument("--policy", default="results/policy.json", help="learned: policy file")
    ap.add_argument("--train", action="store_true", help="learned: explore and update the policy")
    ap.add_argument("--alpha", type=float, default=1.0, help="learned: UCB exploration strength (train)")
    ap.add_argument("--epsilon", type=float, default=0.2, help="learned: random-exploration rate (train)")
    ap.add_argument("--csv", default="results/results.csv")
    a = ap.parse_args()

    strat = make_strategy(a.strategy, a)
    nbytes = int(a.size_mb * 1024 * 1024)
    if hasattr(strat, "seed"):  # learned: one small TCP transfer to get an initial loss estimate
        strat.seed(fetch_tcp(a.host, a.port, "cubic", 512 * 1024))
    os.makedirs(os.path.dirname(a.csv) or ".", exist_ok=True)
    new_file = not os.path.exists(a.csv)
    with open(a.csv, "a", newline="") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["profile", "strategy", "run", "cc_applied", "mbps", "loss", "rtt_ms"])
        for i in range(a.runs):
            t0 = time.perf_counter()
            try:
                r = fetch(a.host, a.port, a.quic_port, strat.next_cc(), nbytes)
            except (asyncio.TimeoutError, OSError, ConnectionError):
                if not hasattr(strat, "on_failure"):
                    # fixed strategy with no fallback: record the failure and move on
                    w.writerow([a.profile, a.strategy, i, "FAILED", "0.00", "", ""])
                    print(f"{a.profile:10} {a.strategy:9} run={i} FAILED (no fallback)", flush=True)
                    continue
                strat.on_failure()  # failed attempt's time still counts against throughput
                r = fetch(a.host, a.port, a.quic_port, strat.next_cc(), nbytes)
            r["mbps_raw"] = r["mbps"]  # transfer-only throughput (excludes a failed QUIC attempt)
            r["mbps"] = nbytes * 8 / (time.perf_counter() - t0) / 1e6
            strat.observe(r)
            w.writerow([a.profile, a.strategy, i, r["cc_applied"],
                        f'{r["mbps"]:.2f}', f'{r["loss"]:.4f}', f'{r["rtt_ms"]:.1f}'])
            print(f'{a.profile:10} {a.strategy:9} run={i} cc={r["cc_applied"]:6} '
                  f'{r["mbps"]:7.2f} Mbps loss={r["loss"]:.4f} rtt={r["rtt_ms"]:.1f}ms', flush=True)


if __name__ == "__main__":
    main()
