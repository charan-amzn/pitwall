# 🏁 Pitwall

**A conversational Formula 1 race-engineer data lab.** Ask a question in plain
English; the **entire coding agent runs inside a per-session AWS Lambda
MicroVM** — the Claude Code CLI, pointed at Amazon Bedrock via the VM's
execution role, writes Python (pandas/matplotlib) and runs it *in the same VM*
over open F1 data. The host process is a thin proxy that streams the tool
trace + a race-engineer readout back to you — with charts.

```
You: Who had the fastest average pace in Miami?

  ┌─ Write /opt/pitwall/pace.py — in the MicroVM
  │ import os, pandas as pd
  │ d = os.environ['PITWALL_DATA']
  │ ...
  └────────────────────────────
  ┌─ Bash ▸ python3 /opt/pitwall/pace.py
  └────────────────────────────
  result [ok]
  │  driverId   mean  count code      team
  │         3 97.456     55  VER  Red Bull Racing

Pitwall: ANT (Mercedes) had the fastest average pace in Miami — edging NOR
(McLaren) by ~0.045s/lap on clean green-flag laps...
```

> Real, current-season data: in 2026 Hamilton drives for **Ferrari**, with Audi
> and Cadillac on the grid - Pitwall reflects whatever races have run when you
> build the image (see [Data](#data)).

## Why this design

A chatbot that just *looks up* facts doesn't need a sandbox — that's a plain
tool call. Pitwall instead has Claude **write and execute analysis code**, which:

- makes answers *grounded in computation* over the data, not recalled from memory;
- produces real artifacts (charts, tables) the model can iterate on;
- creates a genuine need for **execution isolation** — the headline use case
  for Lambda MicroVMs (AI-generated code sandboxes).

Pitwall goes one step further than the usual "host-side agent, VM-side runner"
split: the **entire coding agent** (the Claude Code CLI itself) runs inside
the MicroVM. The host holds no LLM keys and executes no user code. The VM
authenticates to Amazon Bedrock via its own execution role, writes and runs
Python locally, and streams the tool trace back over the VM's HTTPS endpoint.
That way the isolation boundary contains both the model's *decisions* and its
*actions*.

## Prerequisites

- **Python 3.9+** and **git**.
- An **AWS account with the `lambda-microvms` API** enabled, and credentials
  configured (`aws configure`, an `AWS_PROFILE`, or `AWS_ACCESS_KEY_ID` /
  `AWS_SECRET_ACCESS_KEY` env vars). The account/role needs permission to use
  `lambda-microvms:*`, create an S3 bucket, and create an IAM role.
- A **current AWS CLI** (≥ 2.35.11) **or** a boto3 that includes `lambda-microvms`
  (see [Drivers](#drivers-boto3-or-the-aws-cli)). Pitwall uses whichever you have.
- **A supported region.** Lambda MicroVMs is available today in US East (N.
  Virginia, `us-east-1`), US East (Ohio, `us-east-2`), US West (Oregon,
  `us-west-2`), Asia Pacific (Tokyo, `ap-northeast-1`), and Europe (Ireland,
  `eu-west-1`). Set `AWS_REGION` to one of these.
- **Model access:** Amazon Bedrock with a Claude model enabled in your region.
  Bedrock is called by the Claude Code CLI *inside* the MicroVM, via the VM's
  execution role (created for you by `setup_aws.sh`) — no keys on the host.
  New to Bedrock? See [First time on AWS or Bedrock?](#first-time-on-aws-or-bedrock) below.

## First time on AWS or Bedrock?

If you already have AWS credentials and Bedrock model access sorted, skip
ahead to [Quickstart](#quickstart). Otherwise, three one-time steps:

1. **Get AWS credentials.** If you don't already have an IAM user or role with
   access keys, follow AWS's
   [getting started with IAM guide](https://docs.aws.amazon.com/IAM/latest/UserGuide/getting-started.html),
   then run `aws configure` to store them locally.

2. **Enable the Claude model in Bedrock.** Model access isn't on by default —
   you have to request it once per account/region:
   - Open the [Bedrock console](https://console.aws.amazon.com/bedrock/) in
     a supported region (see [Prerequisites](#prerequisites)).
   - Go to **Bedrock configurations → Model access → Modify model access**.
   - Select the Claude model(s) you want (e.g. Claude Opus) and submit. Anthropic
     models on Bedrock are typically approved instantly.
   - See AWS's [walkthrough](https://repost.aws/knowledge-center/bedrock-access-anthropic-model)
     if you get stuck.

3. **Grant the IAM permissions Pitwall needs.** The identity running Pitwall
   drives Lambda MicroVMs, S3, and IAM to build/launch the image; it does **not
   need `bedrock:InvokeModel` itself** — that's on the MicroVM's execution role,
   created by `setup_aws.sh`. Minimal policy for the host identity:

   ```json
   {
     "Version": "2012-10-17",
     "Statement": [
       {
         "Sid": "LambdaMicroVMs",
         "Effect": "Allow",
         "Action": "lambda-microvms:*",
         "Resource": "*"
       },
       {
         "Sid": "BuildInfra",
         "Effect": "Allow",
         "Action": [
           "s3:CreateBucket", "s3:PutObject", "s3:GetObject",
           "s3:HeadBucket", "s3:PutBucketPublicAccessBlock",
           "iam:CreateRole", "iam:GetRole",
           "iam:PutRolePolicy", "iam:UpdateAssumeRolePolicy",
           "sts:GetCallerIdentity"
         ],
         "Resource": "*"
       }
     ]
   }
   ```

   This is intentionally broad (wildcarded resources) to get you running
   quickly. Tighten the `Resource` fields to your specific bucket/role/model
   ARNs before using this in a shared or production account.

## Quickstart

Pitwall runs analysis inside a MicroVM, so there's a one-time build step. It's
**self-healing**: the build script creates any AWS resources it needs (S3 bucket,
IAM role) if they don't exist, and Pitwall finds the built image by name at
runtime - so there are no ARNs to copy around.

```bash
git clone <your-fork-url> pitwall && cd pitwall
python3 -m venv .venv && .venv/bin/python -m pip install -e .
export AWS_REGION=us-west-2          # your region (or set it in .env)

./microvm_image/build_image.sh      # creates infra if missing + builds the image (~3–5 min)
.venv/bin/pitwall                                  # interactive REPL
.venv/bin/pitwall "Who had the fastest average pace in Miami?"   # one-shot
```

That's it - no `.env` is required if `AWS_REGION` and your AWS credentials are in
the environment. (A `.env` is just a convenience; copy `.env.example` to `.env`
to keep config in one place. **Never commit `.env`** — it's git-ignored.)

The first `build_image.sh` fetches real F1 data from OpenF1 (a free, rate-limited
API) and can take a few minutes; if OpenF1 is throttling, the build automatically
falls back to a bundled synthetic dataset so it always completes (see [Data](#data)).

> **Rebuilding weeks later? Force a fresh data fetch.** The build caches the
> CSVs in `microvm_image/data/` so quick iterations on the Dockerfile don't
> hit OpenF1 every time. If you're rebuilding after a couple of race weekends
> and want the new races included, force a re-fetch — otherwise the build
> reuses the stale CSVs:
>
> ```bash
> PITWALL_REBUILD_DATA=1 ./microvm_image/build_image.sh
> # …or nuke the cache directly:
> rm -rf microvm_image/data && ./microvm_image/build_image.sh
> ```
>
> The build's provenance line at the top (e.g. `Races included: 10`) tells
> you what actually went into the image.

## Web UI 🏎️

There's also a pit-wall-styled web UI that renders **charts inline** and shows
the **live MicroVM status** (launching → running, with the VM's endpoint) - the
CLI can't display either. Same MicroVM image.

```bash
.venv/bin/python -m pip install -e ".[web]"   # adds fastapi + uvicorn
.venv/bin/pitwall-web                          # http://127.0.0.1:8000
```

Ask a question in the browser; Claude's code appears as it runs in the VM, charts
render inline, and the engineer "radios back" the readout. The server launches
one MicroVM on the first question and terminates it on shutdown (Ctrl-C). Set
`PITWALL_WEB_HOST` / `PITWALL_WEB_PORT` to change the bind address.

### What it looks like

*"Plot Hamilton's lap times in Austrian GP"* — the header shows the model, the
`agent: Claude Code · in the MicroVM` chip, and the live MicroVM state; each
tool call is tagged as **Claude Code · Bash** or **Claude Code · Write**, so
it's clear the coding agent (not the host) is what's running each step
*inside* the sandbox:

![Pitwall — Claude Code running tool calls inside the MicroVM](docs/images/web-tool-trace.png)

Charts render inline as artifacts stream back from the VM, followed by the
race-engineer readout:

![Pitwall — chart artifact + race-engineer readout](docs/images/web-chart-answer.png)

## Things to ask

The dataset covers whichever races have run this season, so these always work:

- "What team does Hamilton drive for, and how many points has he scored?"
- "Who's leading the drivers' championship, and by how much?"
- "Who had the fastest average pace in the most recent race?"
- "Compare Hamilton's and Leclerc's race pace at Ferrari this season."
- "Plot Hamilton's lap times in his last race and explain the pit stop."
- "Who gained the most positions relative to their grid slot this season?"

## Configuration

| Variable / flag       | Default            | Purpose |
|-----------------------|--------------------|---------|
| `PITWALL_MODEL` / `--model`      | `us.anthropic.claude-opus-4-8` | Bedrock inference-profile id used by Claude Code *inside* the VM. Forwarded per-request, so no rebuild needed to swap. |
| `PITWALL_MICROVM_IMAGE_NAME`     | `pitwall-lab`       | Image looked up by name at runtime (auto-discovered). |
| `PITWALL_MICROVM_IMAGE_ARN`      | —                   | Optional: pin an exact image ARN instead of name lookup. |
| `PITWALL_MICROVM_EXEC_ROLE_ARN`  | *auto*              | Optional: pin the exec role ARN. Auto-derived from your account as `PitwallMicrovmExecRole`. |
| `AWS_REGION`                     | —                   | Required (lambda-microvms + the Bedrock endpoint the VM calls). |
| `--hide-code`                    | off                 | Print only results, not the generated code (CLI). |
| `PITWALL_WEB_HOST` / `PITWALL_WEB_PORT` | `127.0.0.1` / `8000` | Bind address for `pitwall-web`. |

All variables can live in a `.env` file (auto-loaded). The full set of
MicroVM-specific variables (`PITWALL_MICROVM_*`, build settings) is documented in
[`.env.example`](.env.example) and the [Setup](#setup-from-zero-to-a-running-microvm)
section.

> **Model access:** Bedrock is called from *inside* the MicroVM via the exec
> role (`bedrock:InvokeModel*` on `anthropic.*` foundation models + inference
> profiles). Nothing on the host talks to an LLM.

## Data

By default Pitwall uses **real, current-season F1 data** from
**[OpenF1](https://openf1.org)** - a free, key-less API of actual timing data.
The fetch (`pitwall/fetch_openf1.py`) runs once at **image-build time** on your
machine and bakes the CSVs into the MicroVM image, so every session has the data
locally with no runtime network call. Re-run the build to refresh (it picks up
whatever races have completed).

Two sources, selected by `PITWALL_DATA_SOURCE`:

| `PITWALL_DATA_SOURCE` | What you get |
|---|---|
| `openf1` (default) | Real current-season data fetched from OpenF1. Needs network at build time. |
| `synthetic` | Deterministic simulated data (real 2023 grid, fixed-seed timing). Offline; a fallback for building with no network. |

To use your own data instead, drop CSVs with the schema below into
`microvm_image/data/` before building. Schema (Ergast-style, simplified):
`drivers.csv`, `races.csv`, `results.csv`, `pit_stops.csv`, `lap_times.csv` -
see `pitwall/data.py`. With real data, `driverId` is the car number (join on it
to `drivers.csv` for the 3-letter code and team); `grid` and per-lap `position`
may be blank where OpenF1 doesn't provide them.

> **Attribution:** race data from [OpenF1](https://openf1.org) (CC BY 4.0).
> Pitwall is not affiliated with OpenF1, Formula 1, or the FIA; "F1" and related
> marks belong to their owners. For analysis/education only.

## Architecture

```
 CLI / web  ── question ──▶  MicroVMSandbox  ── POST /ask ──▶  MicroVM (per session)
     │                          host proxy                       │
     │                       (no LLM here)                       │  claude -p ...
     │                                                           │  (Bedrock via exec role)
     │                                                           ├─ Write hello.py
     │                                                           ├─ Bash: python3 hello.py
     │                                                           └─ collects artifacts
     │                                                           │
     └── text | tool_use | tool_result | artifact | answer ◀─ NDJSON stream ─┘
```

- **`microvm_image/`** — what runs *inside* the VM:
  `server.py` (accepts `/ask`, spawns `claude -p`, normalizes its
  `stream-json` events, streams NDJSON back), `Dockerfile` (installs the
  Claude Code CLI + pandas/matplotlib + non-root user), `setup_aws.sh` /
  `build_image.sh`.
- **`pitwall/sandbox.py`** — `Sandbox` interface + `AskResult` type.
- **`pitwall/microvm.py`** — `MicroVMSandbox`: launches the VM, POSTs to
  `/ask`, streams events back into user-supplied callbacks. Dual boto3/CLI
  drivers.
- **`pitwall/agent.py`** — a thin host-side proxy that forwards question +
  system prompt into the sandbox and fans events out to the CLI/web renderers.
  **No host-side model client, no host-side tool loop.**
- **`pitwall/config.py`** — env-var / `.env` configuration; auto-derives the
  MicroVM exec role ARN from your account/region.
- **`pitwall/data.py`** — dataset source dispatch + schema (used at build time).
- **`pitwall/fetch_openf1.py`** — real-data fetcher (OpenF1 → CSVs).
- **`pitwall/cli.py`** — REPL / one-shot front door.
- **`pitwall/web.py`** + **`pitwall/static/`** — the pit-wall web UI (FastAPI +
  SSE backend, single-page frontend). Additive front-end over the same `Agent`.

## How the MicroVM sandbox works

Each session runs in its own
[AWS Lambda MicroVM](https://docs.aws.amazon.com/lambda/latest/dg/lambda-microvms-guide.html),
exercising the feature's value proposition directly:

| MicroVM capability | How Pitwall uses it |
|---|---|
| VM-level isolation | The whole coding agent — Claude Code + everything it Bash/Write's — runs in a real Firecracker VM, not your host. Even the model call to Bedrock originates from *inside* the VM. |
| Snapshot fast-boot | Image pre-baked with the Claude Code CLI + python + pandas + the dataset; sessions launch from a snapshot (~5s to RUNNING). |
| Suspend / resume | An idle policy suspends the VM while you think and auto-resumes on your next question — idle sessions stop incurring compute charges. |
| Dedicated HTTPS endpoint | Questions POST to the VM's own endpoint (`X-aws-proxy-auth`); the VM streams events (NDJSON) back over the same connection. No load balancer or connection table. |
| Execution role | Lambda MicroVMs vends temporary AWS credentials to the VM at run time, so the in-VM Claude Code CLI calls Bedrock as `PitwallMicrovmExecRole` without any static keys ever entering the sandbox. |

### Setup: from zero to a running microVM

You need an AWS account with a current AWS
CLI **or** boto3 (see [Drivers](#drivers-boto3-or-the-aws-cli)), and credentials
configured (`aws configure` or env vars). 

```bash
# 1. Install + region.
python3 -m venv .venv && .venv/bin/python -m pip install -e .
export AWS_REGION=us-west-2          # or your region (or put it in .env)

# 2. Build the image. This single command auto-creates the S3 bucket and IAM
#    build role if they don't exist, builds the dataset, runs the Dockerfile on
#    AWS, snapshots it, and polls until ready (a few minutes). No ARNs to copy.
./microvm_image/build_image.sh

# 3. Run — Pitwall finds the image by name automatically.
pitwall "Plot Hamilton's pace in his last race and explain the stop."
```

That's the whole flow. `build_image.sh` calls `setup_aws.sh` for you when needed;
you only run `setup_aws.sh` directly if you want to pre-create the infra
separately. To refresh the data later, run
`PITWALL_REBUILD_DATA=1 ./microvm_image/build_image.sh` — it re-fetches OpenF1
and updates the existing image in place. (Without that env var, the build
reuses the cached CSVs in `microvm_image/data/`.)

**What gets created** (and how to remove it later):

| Resource | Name (default) | Why | Tear down |
|---|---|---|---|
| S3 bucket | `pitwall-microvm-<account>-<region>` | Stages the build zip | `aws s3 rb s3://<bucket> --force` |
| IAM build role | `PitwallMicrovmBuildRole` | Lambda assumes it to build the image | `aws iam delete-role-policy … && aws iam delete-role …` |
| IAM exec role | `PitwallMicrovmExecRole` | Vended to the running MicroVM so Claude Code inside it can call Bedrock | `aws iam delete-role-policy … && aws iam delete-role …` |
| MicroVM image | `pitwall-lab` | The snapshot sessions launch from | `aws lambda-microvms delete-microvm-image --image-identifier <arn>` |

**What happens per session:**
`run-microvm` launches a VM from the snapshot (with `--execution-role-arn`
pointing at `PitwallMicrovmExecRole`) → poll `get-microvm` until `RUNNING` →
`create-microvm-auth-token` mints a scoped token → each question POSTs to
`https://<endpoint>/ask` with the `X-aws-proxy-auth` header → the in-VM server
spawns `claude -p` (which authenticates to Bedrock via the exec role, writes
Python, and runs it with Bash) → events stream back as NDJSON → idle policy
suspends/resumes the VM around your thinking time → `terminate-microvm` on exit.

The in-VM application lives in [`microvm_image/`](microvm_image/): `server.py`
(a stdlib HTTP server exposing `/ask` and the MicroVM lifecycle hooks incl.
`/ready`), `Dockerfile` (on Lambda's snapshot-compatible AL2023 base image; adds
the Claude Code CLI + non-root user + Bedrock env), and the baked dataset.

> 💰 **Cost:** running microVMs incur per-second compute charges; suspended ones
> incur only snapshot storage; terminated ones incur nothing. Pitwall terminates
> the VM on exit and the idle policy auto-suspends while you think. Delete the
> image when done (table above) to stop storage charges.

## License

[MIT](LICENSE). Contributions welcome.
