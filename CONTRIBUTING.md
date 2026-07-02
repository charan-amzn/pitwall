# Contributing to Pitwall

Thanks for your interest! Pitwall is a small, focused project — an F1
race-engineer data lab where Claude writes and runs analysis code inside a
per-session AWS Lambda MicroVM. Contributions are welcome.

## Getting set up

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
```

You can do a lot without any AWS access:

- **Work on the data fetcher** (`pitwall/fetch_openf1.py`): run
  `PITWALL_DATA=/tmp/d .venv/bin/python -m pitwall.fetch_openf1` and inspect the
  CSVs it writes.
- **Work on the in-VM server** (`microvm_image/server.py`): it's a plain stdlib
  HTTP server that spawns the `claude` CLI. If you have `claude` installed
  locally with Bedrock auth working, you can run the whole path off-cloud —
  `PITWALL_DATA=/tmp/d PITWALL_VM_PORT=9999 python microvm_image/server.py` —
  then `POST /ask` with `{"question": "...", "system_prompt": "..."}` and
  watch NDJSON events stream back.
- **Generate the offline dataset**: `PITWALL_DATA_SOURCE=synthetic
  .venv/bin/python -m pitwall.data`.

The full microVM path (`pitwall ...`) requires an AWS account with the
`lambda-microvms` API and a built image — see [README → Setup](README.md).

## Project layout

- `pitwall/` — the agent, sandbox interface, MicroVM backend, model factory,
  config, and data tooling.
- `microvm_image/` — what runs *inside* the VM (`server.py`), the `Dockerfile`,
  and the `setup_aws.sh` / `build_image.sh` scripts.

## Guidelines

- **Keep it dependency-light.** The fetcher and in-VM server use only the Python
  standard library on purpose (the VM image stays small). Don't add third-party
  deps to those without a strong reason.
- **Match the surrounding style.** Type hints, short docstrings, no clever
  one-liners where a plain loop reads better.
- **Don't commit secrets or generated data.** `.env`, `microvm_image/data/`, and
  build artifacts are git-ignored — keep it that way. Use `.env.example` as the
  config template.
- **Be honest in docs.** If something is verified, say so; if it's untested,
  say that too.

## Before opening a PR

- `python -m py_compile pitwall/*.py microvm_image/*.py` passes.
- If you touched the fetcher, run it and confirm the CSVs match the schema in
  `pitwall/data.py`.
- If you touched the microVM path and have AWS access, do one end-to-end run.
- Note in the PR what you tested and what you didn't.

## Data & licensing

Race data comes from [OpenF1](https://openf1.org) (CC BY 4.0). Pitwall is
MIT-licensed (see [LICENSE](LICENSE)). By contributing, you agree your
contributions are licensed under the same terms.
