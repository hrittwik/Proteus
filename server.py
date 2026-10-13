"""Bulk-sender server. The client picks the congestion controller per request.

Protocol:  client -> "GET <cc> <nbytes>\n"
           server -> <nbytes> of payload, then a fixed 128-byte JSON trailer
                     with sender-side stats (RTT, retransmits, loss estimate).
"""
import json
import os
import socket
import struct
import threading
import time

PORT = int(os.environ.get("PORT", 5001))
TRAILER = 128
CHUNK = b"x" * 65536

# Offsets into Linux struct tcp_info (stable kernel ABI prefix)
OFF_SND_MSS, OFF_RTT, OFF_TOTAL_RETRANS = 16, 68, 100


def sender_stats(sock, nbytes):
    buf = sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_INFO, 256)
    mss = struct.unpack_from("<I", buf, OFF_SND_MSS)[0] or 1448
    rtt_us = struct.unpack_from("<I", buf, OFF_RTT)[0]
    retrans = struct.unpack_from("<I", buf, OFF_TOTAL_RETRANS)[0]
    segments = max(1, nbytes // mss)
    return {"rtt_ms": rtt_us / 1000.0, "retrans": retrans, "loss": retrans / segments}


def current_cc(sock):
    raw = sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_CONGESTION, 16)
    return raw.split(b"\0")[0].decode()


def mux_send(conn, parts):
    """MUXMSG <n_total> <ids_csv> <msgs> <size> <interval_ms>
    Sends framed messages [stream_id u32][len u32][payload], payload starts with a
    wall-clock f64 send timestamp. Stream s sends message k at k*interval + s*interval/n,
    so streams are evenly staggered. One connection may carry several streams (HTTP/2-style)."""
    conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    n_total, ids = int(parts[1]), [int(x) for x in parts[2].split(",")]
    msgs, size, interval = int(parts[3]), int(parts[4]), float(parts[5]) / 1000.0
    events = sorted((k * interval + s * interval / n_total, s) for s in ids for k in range(msgs))
    t0 = time.perf_counter()
    for off, sid in events:
        delay = t0 + off - time.perf_counter()
        if delay > 0:
            time.sleep(delay)
        payload = struct.pack("!d", time.time()).ljust(size, b"x")
        conn.sendall(struct.pack("!II", sid, len(payload)) + payload)


def handle(conn):
    with conn:
        line = b""
        while not line.endswith(b"\n"):
            b = conn.recv(1)
            if not b:
                return
            line += b
        parts = line.decode().split()
        if parts and parts[0] == "MUXMSG" and len(parts) == 6:
            mux_send(conn, parts)
            return
        if len(parts) != 3 or parts[0] != "GET":
            return
        cc, nbytes = parts[1], int(parts[2])
        try:
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_CONGESTION, cc.encode())
        except OSError:
            pass  # unavailable algorithm: fall back to default, report what was applied
        applied = current_cc(conn)

        remaining = nbytes
        while remaining > 0:
            n = min(remaining, len(CHUNK))
            conn.sendall(CHUNK[:n])
            remaining -= n

        stats = sender_stats(conn, nbytes)
        stats["cc_applied"] = applied
        conn.sendall(json.dumps(stats).encode().ljust(TRAILER, b" "))


def main():
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", PORT))
    srv.listen(64)
    print(f"listening on :{PORT}", flush=True)
    while True:
        conn, _ = srv.accept()
        threading.Thread(target=handle, args=(conn,), daemon=True).start()


if __name__ == "__main__":
    main()
