"""Production all-in-one process supervisor for Render (API + NATS + workers)."""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from .log import configure_logging
from .settings import get_settings

log = logging.getLogger(__name__)


def _nats_bin() -> str:
    env_bin = os.environ.get("NATS_SERVER_BIN")
    for candidate in (env_bin, "/usr/local/bin/nats-server"):
        if candidate and Path(candidate).is_file():
            return candidate
    return "nats-server"


def main() -> None:
    s = get_settings()
    configure_logging(s.log_level)
    if s.database_url:
        from .auth import migrate

        migrate(s)
        log.info("migrated account schema")

    port = os.environ.get("PORT", "8080")
    nats_store = s.data_dir / "nats"
    nats_store.mkdir(parents=True, exist_ok=True)
    # Point workers at the local NATS started by this process.
    os.environ.setdefault("NATS_URL", "nats://127.0.0.1:4222")

    procs: list[subprocess.Popen] = []

    def start(name: str, args: list[str]) -> None:
        log.info("starting %s: %s", name, " ".join(args))
        procs.append(
            subprocess.Popen(
                args,
                stdout=sys.stdout,
                stderr=sys.stderr,
            )
        )

    start(
        "nats",
        [
            _nats_bin(),
            "-js",
            "-sd",
            str(nats_store),
            "-a",
            "127.0.0.1",
            "-p",
            "4222",
        ],
    )
    time.sleep(1.5)
    start("crawler", [sys.executable, "-m", "cia_brain.worker_crawler"])
    start("feeds", [sys.executable, "-m", "cia_brain.feeds"])
    start("extractor", [sys.executable, "-m", "cia_brain.worker_extractor"])
    start("indexer", [sys.executable, "-m", "cia_brain.worker_indexer"])
    start("scheduler", [sys.executable, "-m", "cia_brain.scheduler"])
    start(
        "api",
        [
            sys.executable,
            "-m",
            "uvicorn",
            "cia_brain.api:app",
            "--host",
            "0.0.0.0",
            "--port",
            str(port),
        ],
    )

    stopping = False

    def _stop(signum, frame):  # noqa: ARG001
        nonlocal stopping
        if stopping:
            return
        stopping = True
        log.info("shutting down children (%s)", signum)
        for proc in procs:
            proc.send_signal(signal.SIGTERM)
        deadline = time.time() + 20
        for proc in procs:
            remaining = max(0.1, deadline - time.time())
            try:
                proc.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                proc.kill()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    while not stopping:
        for proc in procs:
            code = proc.poll()
            if code is not None and not stopping:
                log.error("child exited early pid=%s code=%s", proc.pid, code)
                _stop(signal.SIGTERM, None)
                raise SystemExit(code or 1)
        time.sleep(1)


if __name__ == "__main__":
    main()
