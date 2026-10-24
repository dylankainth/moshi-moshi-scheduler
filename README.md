# Foresight

You guess how long tasks take, you're wrong in the same direction every time, and you never find out by how much.
Foresight closes the loop: **estimate → watch → compare → learn**.

1. **Estimate.** Describe the task (and optionally your guess). You get a median, 50% and 80% ranges, and the past tasks behind them, in ~30 ms.
2. **Watch.** A local agent samples the active window and idle state every 3 s and works out which time belongs to the task.
3. **Compare.** Press done: Foresight's range vs your guess vs the actual. Fix the actual if needed, then confirm.
4. **Learn.** Only confirmed actuals update a conjugate Bayesian model, which also learns your bias ("you take 1.6x what you say").

Pure Python standard library: no dependencies to install, nothing to audit, a tiny container.

## Run it

```powershell
python -m foresight run                     # local mode: http://127.0.0.1:8765, capture agent included
python -m foresight run --inference ollama  # + local LLM judge/explainer via Ollama on 127.0.0.1
python -m foresight check                   # print what capture sees for 30 s (writes nothing)
python -m unittest                          # 44 tests
```

The first launch comes with seeded history (a simulated user with a 1.6x bias), so estimates and calibration work
immediately. **Try the scripted demo** in the header replays a 57-minute work session at 30x speed: editor, tests, a Slack
check, Stack Overflow, lunch, a push, then Reddit.

Data lives in `%LOCALAPPDATA%\Foresight` (`foresight.db`, `ignore.json`). Override it with `FORESIGHT_HOME`.

## How it decides what counts

Attribution is a pure function of the raw samples since the task started. It's recomputed every 5 s, so it **finalises
retroactively**, and every decision keeps its source (`rule`, `embedding`, `llm`, `user`, `default`):

| Step | What |
|---|---|
| Rules | Locked screen, gaps (sleep, agent off) and 5+ min idle never count. Same file/repo/page as earlier on-task work = on. Known distraction sites = off. Private (ignored) windows are off but can be detours. |
| Embedding | Similarity between the task text and the window text (offline feature-hashing embedder; `FORESIGHT_EMBEDDER=st` for sentence-transformers). |
| LLM | Only for stretches that are still ambiguous **and** at least 60 s long. Answers ON/OFF with a short reason. Never produces durations. |
| Runs | An off-task run of 5 min or less between on-task work is a detour and counts. Longer runs don't. A trailing run is *pending* until it resolves. 15+ min of trailing off-task time asks "done with X?". |

Thresholds are in `foresight/attribution.py:Cfg`; override any of them with `FORESIGHT_<NAME>` in `.env` (see `.env.example`).

## The model

`log d ~ Normal(mu_c, sigma_c²)` per task type with a normal-inverse-gamma prior centred on the global fit (partial
pooling, so new types borrow strength). With a guess it's a conjugate Bayesian linear regression,
`log d = a_c + b_c·(log g − c)`, with priors centred on the pooled fit. Predictions are the Student-t posterior predictive,
converted to minutes. Updates are sufficient-statistic adds: instant, closed form, no MCMC. Bias = `exp(a + (b−1)·log g)`
at your median guess. Tests check that it recovers a true 1.6x bias (1.56–1.62 across seeds), that online 80% ranges hit
about 80% (77–80%), and that a new type's range narrows from 9.6x to 3.2x after 10 tasks.

## Privacy

- **Never collected, in any mode:** screenshots, keystrokes, clipboard.
- **Ignore list** (banking, password managers, messaging, email, InPrivate/Incognito; editable in the UI or in
  `ignore.json`): matching windows become an anonymous `private · category` sample *before* anything touches disk.
  The app name, title and URL are never written or sent anywhere, and a test scans the raw DB bytes to prove it. Their
  time still follows the detour rules, so a quick Slack check counts without Slack being read.
- **Pause** (paused time is a gap: no data) and **delete-all** (drops every table, then `VACUUM`).
- **Local mode:** the API binds to 127.0.0.1 only, checks the Host header (DNS-rebinding guard) and serves a strict CSP (the page
  can't load anything external). `tests/test_privacy_net.py` runs the whole flow with `socket.connect`/`getaddrinfo`
  patched and fails on any non-loopback attempt.

## Best Use of Vultr

Cloud mode is the same server app, deployed to Vultr, with your capture agent still running on your PC.

| Runs where | What |
|---|---|
| **Your PC** (`python -m foresight agent --server https://… --token …`) | Win32 capture, raw samples in local SQLite, ignore list, pause, rules + embedding attribution. Sends **only** the active task id, attributed segments (`start, end, counted, source, reason, kind`) and redacted snippets (emails and long numbers stripped, 140 chars) for stretches that stay ambiguous. A test with a recording mock server enforces this. |
| **Vultr Cloud Compute** (Docker Compose: app + Caddy) | Model, history, UI, calibration, and the public **read-only demo user**: anyone opening the URL gets a private, scripted replay session, so judges see the whole loop without installing anything. Auto-backups are enabled on the instance. |
| **Vultr Serverless Inference** (OpenAI-compatible, `https://api.vultrinference.com/v1`) | (1) the ambiguity judge for the few stretches rules and embeddings can't settle; (2) the plain-language explanation of each estimate. The model is picked at startup from `GET /v1/models`, never hardcoded. If a call fails, the judge falls back to rules and the explanation falls back to the template. |

**Why Vultr is in the design and not bolted on:**
- **Cheapest first, shown live.** The System tab shows how many stretches were settled by rules vs embedding vs LLM,
  plus calls, tokens and latency. The LLM gets a small, already-redacted share of the work.
- **Clean split.** The OpenAI-compatible endpoint lets one provider abstraction (`none | ollama | vultr`) cover local
  and cloud, and local mode *refuses* `vultr`.
- **Real deployment.** It's a deployed service with HTTPS, a firewall and backups, provisioned by one command.

### Deploy

```powershell
$env:VULTR_API_KEY="…"; $env:VULTR_INFERENCE_KEY="…"
python deploy/vultr/provision.py --repo https://github.com/<you>/<repo> --ssh-cidr <your.ip>/32           # dry run: prints every request
python deploy/vultr/provision.py --repo https://github.com/<you>/<repo> --ssh-cidr <your.ip>/32 --apply   # creates it (billed)
```

To use OpenAI instead, set `OPENAI_API_KEY` (and optionally `OPENAI_MODEL`, default `gpt-4o-mini`) in place of
`VULTR_INFERENCE_KEY`. The server picks OpenAI whenever that key is set; `--inference` or `FORESIGHT_INFERENCE` overrides this.

It creates, in order:
1. A firewall group with 80/443 open (v4 and v6), and SSH only from `--ssh-cidr` (closed if you leave it out).
2. A boot startup script that installs Docker and runs `docker compose up` (app + Caddy).
3. An Ubuntu 24.04 instance with `backups: enabled`.

Caddy gets a Let's Encrypt certificate for `<ip-dashed>.sslip.io`, or for `--domain`. Secrets (owner token, inference key) go
in the instance's `user_data`, not the account-wide startup script, and are never printed. The script prints your
owner link (`https://host/#token=…`) once. `/api/health` reports mode, version, inference provider and model, DB size and
uptime.

Request bodies follow Vultr API v2 as implemented by Vultr's official `govultr`/`vultr-cli`. vultr.com/api blocked
automated fetching, so the docs page itself wasn't checked. `tests/test_provision.py` checks the dry run and the full apply
sequence against mocked HTTP.

## Status: verified vs not

**Verified here:**
- All 44 tests pass.
- Live Win32 capture on Windows 11 x64: exe, title and idle state read correctly, and the real agent attributed live
  activity to a task.
- Cloud `serve` mode runs: demo replay, owner-token gating and health all work.

**Not verified:**
- An actual Vultr deploy, and the Docker image build (Docker isn't installed on the dev machine).
- Real Serverless Inference responses (tested against an OpenAI-compatible fake).
- The Vultr metadata URLs used in `startup.sh` (it falls back to `hostname -I`).
- Screen-lock and UWP capture on real hardware (logic unit-tested).
- The UI in a real browser (JS syntax-checked; served and API-tested).

**Skipped (stretch):** nightly SQLite backup to Vultr Object Storage. The instance's auto-backups cover the VM meanwhile.
