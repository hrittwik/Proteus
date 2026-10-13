# Proteus

**A lab for testing whether the internet's "languages" can be swapped on the fly, and whether a computer can learn when to swap them.**

Named after Proteus, a Greek sea god who could change shape at will.

---

## The idea in plain English

When two computers talk over the internet, they follow a set of rules called a **protocol**, a bit like agreeing on a language and etiquette before a conversation. There is no single protocol that is best for everything:

- Streaming a video wants steady, high speed.
- A video call wants low delay more than perfect accuracy.
- A bad Wi-Fi or mobile connection loses data and needs a protocol that recovers quickly.

Today most programs pick one protocol up front and stick with it. **Proteus asks: what if the computer measured the connection, picked the best protocol for the moment, and learned from experience which one works best where?**

This is ordinary (classical) networking. It has nothing to do with quantum computing.

## What Proteus does

Proteus builds a small test network on your own computer, makes it behave badly on purpose (slow, lossy, congested), and measures how different protocol choices perform. It runs three experiments:

| Experiment | The question | Command |
|---|---|---|
| **Speed test** | Which protocol moves data fastest on which kind of network? | `python run_matrix.py` |
| **Many-streams test** | When many conversations share one connection, does one lost packet slow all of them down? | `python run_mux.py` |
| **Blocked-network test** | If a network blocks the newer protocol, can the client notice and fall back to the older one? | `python run_udp_blocked.py` |

And one training step:

| Step | What it does | Command |
|---|---|---|
| **Learn** | Teaches a small AI "switcher" which protocol to use under which conditions | `python train_policy.py` |

## The protocols being compared

- **TCP Cubic**: the long-time default of the internet. Reliable, but it slows down a lot when it sees data loss.
- **TCP BBR**: a newer approach from Google that estimates the connection's real capacity instead of reacting to every loss. Often better on lossy links.
- **QUIC**: the modern protocol behind HTTP/3. Built on UDP, it can carry many independent conversations at once without one holding up the others.

## How the "learning" works

The switcher is a **contextual bandit**, a simple kind of reinforcement learning:

1. Before each transfer it measures the connection (delay and recent data loss).
2. It chooses a protocol.
3. It sees how fast the transfer went and updates its beliefs.
4. During training it deliberately tries every option, including ones that look worse, so it doesn't get stuck on a bad habit.
5. After training, it stops exploring and just picks what it has learned is best.

It is trained on one set of network conditions and tested on different ones, to check it can handle situations it hasn't seen. If QUIC fails to connect at all (for example, a network blocks it), the switcher marks QUIC unavailable and moves on rather than learning the wrong lesson.

## Use cases

Proteus is a research and testing prototype, not something you deploy. It is useful for:

**Practical**
- **Deciding whether to enable BBR or HTTP/3 on your servers.** Recreate network conditions that match your users (mobile, rural, congested Wi-Fi) and see which protocol wins before changing production.
- **Testing apps under bad networks.** The Docker and netem setup gives reproducible delay, loss and bandwidth limits, handy for QA on mobile apps, video calls, or anything that must survive flaky connections.
- **Testing fallback behavior.** Many corporate and school networks block UDP, which breaks QUIC. The blocked-network test checks that a client degrades gracefully to TCP instead of hanging.
- **Measuring head-of-line blocking.** The many-streams test shows how much one lost packet hurts multiplexed traffic (like HTTP/2) compared with QUIC.

**Research and learning**
- A small, readable baseline for learned protocol selection: swap in a better learner and compare it against the fixed choices.
- A teaching lab for congestion control, QUIC and bandit-style reinforcement learning, runnable on a laptop.

**Where the same idea appears in the real world**
- CDNs and video platforms tune congestion control per client or region.
- Browsers race or fall back between HTTP/3 and HTTP/2.
- Satellite, rural and IoT links, where one fixed protocol performs poorly.

Real deployments would choose protocols per connection inside the kernel or a load balancer. Proteus is a small Python simulation, so its results show trends, not guarantees.

## Running it

**You need:** Linux with Docker and Docker Compose, plus Python 3. For BBR, run `sudo modprobe tcp_bbr` once on the host.

```bash
python train_policy.py          # learn (about 10 minutes)
python run_matrix.py            # speed test, including the learned switcher
python run_mux.py               # many-streams test
python run_udp_blocked.py       # blocked-network test
python learned.py results/policy.json   # see what the switcher learned
```

Raw numbers are saved to `results/*.csv`, and each script prints a summary table when it finishes.

## What's in the box

| File | Role |
|---|---|
| `server.py`, `quic_server.py` | The sending side (TCP and QUIC) |
| `client.py` | The receiving side and the protocol-choosing strategies |
| `learned.py` | The learning switcher |
| `mux_client.py` | Client for the many-streams test |
| `run_*.py`, `train_policy.py` | Scripts that set up the fake bad networks and run the experiments |
| `Dockerfile`, `docker-compose.yml` | The containers everything runs in |

## Honest limitations

- **Not yet run end to end.** The code was tested on a single machine without the simulated bad networks, so the experiment results don't exist yet. Running them is the next step.
- **QUIC is slow here.** The QUIC library used (aioquic) is written in Python, so it tops out around 50 to 75 Mbps and has no BBR. Don't read a QUIC loss in the speed test as "QUIC is slower than TCP".
- **One piece is untested.** The rule that blocks UDP in the blocked-network test hasn't been validated.
- **It decides per transfer.** It does not adapt in the middle of a transfer.
- **Simulation is not the real internet.** Results show trends under controlled conditions, not guarantees for real networks.

## Words you might need

- **Protocol**: an agreed set of rules for how computers talk.
- **Packet loss**: data that gets dropped in transit and must be resent.
- **Latency / RTT**: the delay for a message to travel there and back.
- **Head-of-line blocking**: when one delayed piece of data holds up everything queued behind it.
- **Congestion control**: how a protocol decides how fast to send without overwhelming the network.
- **Netem**: a Linux tool that simulates delay, loss and bandwidth limits.
