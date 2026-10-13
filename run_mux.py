"""Run the multi-stream HOL-blocking test: profile x mode, netem on server egress."""
import csv
import os
import sys
from collections import defaultdict

from run_matrix import sh

PROFILES = {
    "clean":   "delay 20ms",
    "lossy_1": "delay 40ms loss 1%",
    "lossy_3": "delay 40ms loss 3%",
}
MODES = ["tcp-mux", "tcp-parallel", "quic"]
RUNS = 3
CSV_PATH = "results/mux.csv"


def main():
    if os.path.exists(CSV_PATH):
        os.remove(CSV_PATH)
    sh("docker", "compose", "up", "-d", "--build", "server")
    for name, netem in PROFILES.items():
        sh("docker", "compose", "exec", "-T", "server", "tc", "qdisc", "replace",
           "dev", "eth0", "root", "netem", *netem.split())
        for mode in MODES:
            sh("docker", "compose", "run", "--rm", "client", "python", "mux_client.py",
               "--mode", mode, "--profile", name, "--runs", str(RUNS))
    sh("docker", "compose", "down", check=False)
    summarise()


def summarise():
    acc = defaultdict(lambda: defaultdict(list))
    with open(CSV_PATH) as f:
        for r in csv.DictReader(f):
            for k in ("p50_ms", "p95_ms", "p99_ms"):
                acc[(r["profile"], r["mode"])][k].append(float(r[k]))
    print(f'\n{"profile":9} {"mode":13} {"p50":>7} {"p95":>7} {"p99":>7}   (mean over runs, ms)')
    for p in PROFILES:
        for m in MODES:
            d = acc.get((p, m))
            if d:
                avg = lambda k: sum(d[k]) / len(d[k])
                print(f'{p:9} {m:13} {avg("p50_ms"):7.1f} {avg("p95_ms"):7.1f} {avg("p99_ms"):7.1f}')


if __name__ == "__main__":
    sys.exit(main())
