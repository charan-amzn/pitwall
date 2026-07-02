# MicroVM application image for Pitwall's agent+analysis sandbox.
#
# What runs in this MicroVM:
#   1. The in-VM HTTP server (server.py) that accepts /ask.
#   2. The Claude Code CLI itself — the CODING AGENT runs here, not on the
#      host. Given a question, `claude -p` writes Python, runs it via Bash,
#      and streams the trace back to server.py, which forwards it to the host.
#   3. pandas/matplotlib for the Python the agent writes.
#
# Lambda snapshots this fully-initialized image, so the CLI is downloaded and
# heavy Python imports are paid once at build time, not per launch. The
# container FROM here is unrelated to the Lambda *MicroVM* base image (OS +
# service components) supplied separately via --base-image-arn.
FROM public.ecr.aws/lambda/microvms:al2023-minimal

# --- system tools + Python -------------------------------------------------
# curl-minimal is already present on al2023-minimal (don't `dnf install curl`,
# it conflicts with curl-minimal). tar/gzip are for the Claude Code installer;
# shadow-utils gives us useradd; ripgrep comes bundled with the CLI installer.
RUN dnf install -y python3 python3-pip tar gzip shadow-utils && dnf clean all
RUN pip3 install --no-cache-dir "pandas>=2.0" "numpy>=1.26" "matplotlib>=3.7"

# --- non-root user ---------------------------------------------------------
# Claude Code refuses --dangerously-skip-permissions / bypassPermissions when
# uid == 0. The image must therefore run as a non-root user. `pitwall` owns
# /opt/pitwall and its own $HOME (~/.claude lives there).
RUN useradd -m -u 1000 -s /bin/bash pitwall

# --- Claude Code (native installer) ----------------------------------------
# The native installer drops a self-contained binary in $HOME/.local/bin and
# needs no Node runtime. We install as `pitwall` so its config paths live
# under /home/pitwall.
USER pitwall
ENV HOME=/home/pitwall \
    PATH=/home/pitwall/.local/bin:/usr/local/bin:/usr/bin:/bin
RUN curl -fsSL https://claude.ai/install.sh | bash

# --- app -------------------------------------------------------------------
USER root
RUN mkdir -p /opt/pitwall/data && chown -R pitwall:pitwall /opt/pitwall
COPY --chown=pitwall:pitwall server.py /opt/pitwall/server.py
COPY --chown=pitwall:pitwall data/ /opt/pitwall/data/

USER pitwall
WORKDIR /opt/pitwall

ENV PITWALL_DATA=/opt/pitwall/data \
    PITWALL_VM_PORT=8080 \
    PITWALL_CLAUDE_BIN=/home/pitwall/.local/bin/claude \
    MPLBACKEND=Agg \
    CLAUDE_CODE_USE_BEDROCK=1 \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
    DISABLE_TELEMETRY=1 \
    DISABLE_AUTOUPDATER=1 \
    DISABLE_ERROR_REPORTING=1

EXPOSE 8080

# Pre-warm heavy imports and confirm the claude binary exists — both details
# then land in the snapshot, so runtime launches skip them.
CMD ["python3", "-c", "import pandas, numpy, matplotlib.pyplot; import subprocess; subprocess.run(['claude', '--version'], check=False); import server; server.main()"]
