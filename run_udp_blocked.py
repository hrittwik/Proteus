"""UDP-blocked scenario: drop all UDP arriving at the server (a tc ingress filter), so
QUIC can't connect. Compares:
  quic        fixed QUIC, no fallback  -> every run should FAIL
  quic-first  QUIC with TCP fallback   -> first run pays the timeout, then uses TCP
  cubic       plain TCP baseline
Netem delay/rate is applied on egress so the TCP path is realistic."""
import csv
import os
import sys

from run_matrix import sh

NETEM = "delay 40ms rate 50mbit"
STRATEGIES = ["quic", "quic-first", "cubic"]
if os.path.exists("results/policy.json"):
    STRATEGIES.append("learned")
QUIC_TIMEOUT = "3"          # seconds the client waits for a QUIC handshake
RUNS, SIZE_MB = 6, 2
CSV_PATH = "results/udp_blocked.csv"
TC = ["docker", "compose", "exec", "-T", "server", "tc"]


def main():
    if os.path.exists(CSV_PATH):
        os.remove(CSV_PATH)
    sh("docker", "compose", "up", "-d", "--build", "server")
    sh(*TC, "qdisc", "replace", "dev", "eth0", "root", "netem", *NETEM.split())
    sh(*TC, "qdisc", "replace", "dev", "eth0", "ingress")
    sh(*TC, "filter", "add", "dev", "eth0", "parent", "ffff:", "protocol", "ip",
       "prio", "1", "u32", "match", "ip", "protocol", "17", "0xff", "action", "drop")
    try:
        for strat in STRATEGIES:
            sh("docker", "compose", "run", "--rm", "-e", f"QUIC_TIMEOUT={QUIC_TIMEOUT}",
               "client", "python", "client.py", "--strategy", strat, "--profile", "udp_blocked",
               "--runs", str(RUNS), "--size-mb", str(SIZE_MB), "--csv", CSV_PATH)
    finally:
        sh(*TC, "qdisc", "del", "dev", "eth0", "ingress", check=False)
        sh("docker", "compose", "down", check=False)
    summarise()


def summarise():
    rows = list(csv.DictReader(open(CSV_PATH)))
    print("\nstrategy    ok/runs   first-run Mbps   later-runs mean Mbps")
    for strat in STRATEGIES:
        r = [x for x in rows if x["strategy"] == strat]
        ok = [x for x in r if x["cc_applied"] != "FAILED"]
        first = float(r[0]["mbps"]) if r else 0.0
        later = [float(x["mbps"]) for x in ok[1:]] if ok and r[0] in ok else [float(x["mbps"]) for x in ok]
        mean = sum(later) / len(later) if later else 0.0
        print(f"{strat:11} {len(ok)}/{len(r):<7} {first:14.2f} {mean:20.2f}")


if __name__ == "__main__":
    sys.exit(main())
