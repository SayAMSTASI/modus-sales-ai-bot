from __future__ import annotations

import argparse
import subprocess
import sys
import time

import httpx


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Start the API and verify live/readiness endpoints"
    )
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(args.port),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise SystemExit(f"API exited early with code {process.returncode}")
            try:
                live = httpx.get(f"http://127.0.0.1:{args.port}/health/live", timeout=1)
                ready = httpx.get(f"http://127.0.0.1:{args.port}/health/ready", timeout=1)
                if live.json() == {"status": "ok"} and ready.json() == {"status": "ready"}:
                    print("http-smoke-ok")
                    return
            except (httpx.HTTPError, ValueError):
                pass
            time.sleep(0.25)
        raise SystemExit("API did not become ready within 15 seconds")
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


if __name__ == "__main__":
    main()
