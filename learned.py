"""Learned protocol switcher: LinUCB contextual bandit (pure Python, no deps).

Arms      : cubic, bbr (TCP congestion controllers) and quic.
Context   : [1, f_rtt, f_loss, f_rtt*f_loss], log-scaled path RTT and loss.
            (log scale because TCP throughput ~ 1/(RTT*sqrt(loss)) is linear in logs)
Reward    : ln(throughput Mbps) - ln(20). Differences between arms are then *ratios*, which
            makes the reward comparable across links of very different capacity.
Policy    : per-arm ridge regression, score = theta.x + alpha*sqrt(x' A^-1 x).
            Train mode: forced warm-up pulls per arm, then epsilon-random + UCB, and updates.
            Eval mode: greedy and frozen.
Failures  : a QUIC handshake failure (UDP blocked) is an availability problem, not a
            performance sample: the arm is masked for the session and the model is NOT
            updated, so one blocked network can't poison QUIC's estimate for clean ones.

  python learned.py results/policy.json     # print the learned policy
"""
import json
import math
import os
import random
import sys

ARMS = ["cubic", "bbr", "quic"]
D = 4
REWARD_CENTER = math.log(20.0)  # centre rewards near typical throughput: with theta=0 priors,
                                # raw ln(Mbps) makes every tried arm look better than any untried one
MIN_PULLS = 3                   # train mode: every available arm gets this many pulls first


def features(rtt_ms, loss):
    f1 = min(math.log1p(max(rtt_ms, 0.0)) / math.log1p(400.0), 1.5)
    f2 = min(math.log1p(max(loss, 0.0) * 1000.0) / math.log1p(50.0), 1.5)
    return [1.0, f1, f2, f1 * f2]


def reward(mbps):
    return math.log(max(mbps, 0.05)) - REWARD_CENTER


class LinUCB:
    def __init__(self, arms=ARMS, d=D, lam=1.0, alpha=1.0):
        self.arms, self.d, self.alpha = list(arms), d, alpha
        self.m = {a: {"A_inv": [[(1.0 / lam) if i == j else 0.0 for j in range(d)]
                                for i in range(d)],
                      "b": [0.0] * d, "n": 0} for a in self.arms}

    def theta(self, a):
        A, b = self.m[a]["A_inv"], self.m[a]["b"]
        return [sum(A[i][j] * b[j] for j in range(self.d)) for i in range(self.d)]

    def score(self, a, x, alpha=None):
        alpha = self.alpha if alpha is None else alpha
        A = self.m[a]["A_inv"]
        mean = sum(t * xi for t, xi in zip(self.theta(a), x))
        Ax = [sum(A[i][j] * x[j] for j in range(self.d)) for i in range(self.d)]
        var = sum(x[i] * Ax[i] for i in range(self.d))
        return mean + alpha * math.sqrt(max(var, 0.0))

    def choose(self, x, available=None, alpha=None):
        cands = list(available) if available else self.arms
        return max(cands, key=lambda a: self.score(a, x, alpha))

    def update(self, a, x, r):
        """Sherman-Morrison rank-1 update of A^-1; A^-1 stays symmetric."""
        s = self.m[a]
        A = s["A_inv"]
        Ax = [sum(A[i][j] * x[j] for j in range(self.d)) for i in range(self.d)]
        denom = 1.0 + sum(x[i] * Ax[i] for i in range(self.d))
        for i in range(self.d):
            for j in range(self.d):
                A[i][j] -= Ax[i] * Ax[j] / denom
            s["b"][i] += r * x[i]
        s["n"] += 1

    def save(self, path):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"arms": self.arms, "d": self.d, "alpha": self.alpha, "models": self.m}, f)
        os.replace(tmp, path)

    @classmethod
    def load(cls, path):
        with open(path) as f:
            j = json.load(f)
        obj = cls(j["arms"], j["d"], alpha=j.get("alpha", 1.0))
        obj.m = j["models"]
        return obj


class Learned:
    """Strategy plugged into client.py (next_cc / observe / seed / on_failure)."""

    def __init__(self, policy_path, train, alpha, probe_rtt, epsilon=0.2):
        self.path, self.train, self.probe_rtt, self.eps = policy_path, train, probe_rtt, epsilon
        self.rng = random.Random()
        if os.path.exists(policy_path):
            self.model = LinUCB.load(policy_path)
        elif train:
            self.model = LinUCB(alpha=alpha)
        else:
            raise SystemExit(f"no policy at {policy_path}; run train_policy.py first")
        self.alpha = alpha if train else 0.0
        self.loss, self.last, self.blocked = None, None, set()

    def seed(self, r):
        self.loss = r["loss"]

    def next_cc(self):
        x = features(self.probe_rtt(), self.loss or 0.0)
        avail = [a for a in self.model.arms if a not in self.blocked]
        under = [a for a in avail if self.model.m[a]["n"] < MIN_PULLS]
        if self.train and under:                       # warm-up: least-pulled arm first
            arm = min(under, key=lambda a: self.model.m[a]["n"])
        elif self.train and self.rng.random() < self.eps:  # scale-free random exploration
            arm = self.rng.choice(avail)
        else:
            arm = self.model.choose(x, avail, self.alpha)
        self.last = (arm, x)
        return arm

    def observe(self, r):
        arm, x = self.last
        self.loss = r["loss"] if self.loss is None else 0.5 * r["loss"] + 0.5 * self.loss
        if self.train:
            # raw transfer throughput: excludes time wasted on a failed QUIC attempt
            self.model.update(arm, x, reward(r.get("mbps_raw", r["mbps"])))
            self.model.save(self.path)

    def on_failure(self):
        if self.last and self.last[0] == "quic":
            self.blocked.add("quic")


def show(path):
    m = LinUCB.load(path)
    print("pulls per arm:", {a: m.m[a]["n"] for a in m.arms})
    rtts, losses = [10, 40, 100, 150, 200], [0.0, 0.005, 0.01, 0.02, 0.04]
    print("\ngreedy choice by path conditions (rows: loss, cols: RTT ms)")
    print(f'{"loss":>7} ' + " ".join(f"{r:>7}" for r in rtts))
    for l in losses:
        cells = [m.choose(features(r, l), alpha=0.0) for r in rtts]
        print(f"{l*100:6.1f}% " + " ".join(f"{c:>7}" for c in cells))


if __name__ == "__main__":
    show(sys.argv[1] if len(sys.argv) > 1 else "results/policy.json")
