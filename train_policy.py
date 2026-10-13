"""Train the learned switcher across randomly sampled network conditions.

Training profiles are drawn from value grids that DON'T overlap the evaluation
profiles in run_matrix.py (delay 10/40/100/150 ms, loss 0/1/3 %), so evaluation
measures generalisation to unseen conditions, not memorisation.

  python train_policy.py                      # 16 profiles x 10 transfers
  python train_policy.py --profiles 24 --runs 12 --reset
"""
import argparse
import os
import random

from run_matrix import sh

POLICY = "results/policy.json"
DELAYS, LOSSES, RATES = [20, 60, 120, 200], [0, 0.3, 0.7, 1.5, 2, 4], [10, 30, 80]


def sample_profiles(k, seed):
    combos = [(d, l, r) for d in DELAYS for l in LOSSES for r in RATES]
    random.Random(seed).shuffle(combos)
    return combos[:k]


def netem_args(delay, loss, rate):
    args = ["delay", f"{delay}ms"]
    if loss:
        args += ["loss", f"{loss}%"]
    return args + ["rate", f"{rate}mbit"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profiles", type=int, default=16)
    ap.add_argument("--runs", type=int, default=10, help="transfers per profile")
    ap.add_argument("--size-mb", type=float, default=5)
    ap.add_argument("--alpha", type=float, default=1.0)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--reset", action="store_true", help="discard any existing policy")
    a = ap.parse_args()

    if a.reset:
        for p in (POLICY, "results/train.csv"):
            if os.path.exists(p):
                os.remove(p)
    sh("docker", "compose", "up", "-d", "--build", "server")
    for i, (d, l, r) in enumerate(sample_profiles(a.profiles, a.seed)):
        sh("docker", "compose", "exec", "-T", "server", "tc", "qdisc", "replace",
           "dev", "eth0", "root", "netem", *netem_args(d, l, r))
        sh("docker", "compose", "run", "--rm", "client", "python", "client.py",
           "--strategy", "learned", "--train", "--alpha", str(a.alpha),
           "--profile", f"train{i}_d{d}_l{l}_r{r}", "--runs", str(a.runs),
           "--size-mb", str(a.size_mb), "--csv", "results/train.csv")
    sh("docker", "compose", "down", check=False)
    from learned import show
    show(POLICY)


if __name__ == "__main__":
    main()
