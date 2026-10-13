"""Run profile x strategy matrix. Netem is applied on the server's egress
(the server is the data sender), then results are summarised."""
import csv
import os
import subprocess
import sys
from collections import defaultdict

PROFILES = {
    "clean":    "delay 10ms rate 100mbit",
    "high_rtt": "delay 100ms rate 50mbit",
    "lossy":    "delay 40ms loss 1% rate 50mbit",
    "bad":      "delay 150ms loss 3% rate 20mbit",
}
STRATEGIES = ["cubic", "bbr", "quic", "adaptive"]
if os.path.exists("results/policy.json"):   # frozen greedy policy from train_policy.py
    STRATEGIES.append("learned")
RUNS, SIZE_MB = 8, 5
CSV_PATH = "results/results.csv"


def sh(*cmd, check=True, capture=False):
    return subprocess.run(cmd, check=check, text=True, capture_output=capture)


def main():
    if os.path.exists(CSV_PATH):
        os.remove(CSV_PATH)
    sh("docker", "compose", "up", "-d", "--build", "server")

    avail = sh("docker", "compose", "exec", "-T", "server", "cat",
               "/proc/sys/net/ipv4/tcp_available_congestion_control", capture=True).stdout
    if "bbr" not in avail:
        print(f"WARNING: bbr not available (have: {avail.strip()}). "
              "On the host try: sudo modprobe tcp_bbr", file=sys.stderr)

    for name, netem in PROFILES.items():
        sh("docker", "compose", "exec", "-T", "server", "tc", "qdisc", "replace",
           "dev", "eth0", "root", "netem", *netem.split())
        for strat in STRATEGIES:
            sh("docker", "compose", "run", "--rm", "client", "python", "client.py",
               "--strategy", strat, "--profile", name,
               "--runs", str(RUNS), "--size-mb", str(SIZE_MB))

    sh("docker", "compose", "down", check=False)
    summarise()


def summarise():
    data = defaultdict(list)
    with open(CSV_PATH) as f:
        for row in csv.DictReader(f):
            data[(row["profile"], row["strategy"])].append(float(row["mbps"]))
    print(f'\n{"profile":10} ' + " ".join(f"{s:>10}" for s in STRATEGIES) + "   (mean Mbps)")
    for p in PROFILES:
        cells = []
        for s in STRATEGIES:
            v = data.get((p, s), [])
            cells.append(f"{sum(v)/len(v):10.2f}" if v else f'{"n/a":>10}')
        print(f"{p:10} " + " ".join(cells))


if __name__ == "__main__":
    main()
