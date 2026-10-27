"""
Runs the app the way the public server does: with Waitress, a server
program made for real use, instead of Flask's development server
(`python app.py`, which stays the way to work on the app).

One process with several threads: the app keeps its search index, its
short-lived copies of the timetable's answers and the trips it has shown
in memory, and every request shares them.

Settings come from the environment, or from a file `.env` next to this one
(see `.env.example`); the environment wins:

  BW_HOST     where to listen: 127.0.0.1 behind Caddy (the default),
              0.0.0.0 to be reachable from other devices
  BW_PORT     the port (default 8000)
  BW_THREADS  requests served at the same time (default 8)
  BW_PROXY    the address of the proxy in front (Caddy: 127.0.0.1) whose
              X-Forwarded-For / -Proto headers are to be believed; empty
              without one

  python serve.py
"""

import logging
import os
from pathlib import Path

from waitress import serve

HERE = Path(__file__).resolve().parent


def read_env_file(path: Path) -> dict:
    """KEY=VALUE lines of a .env file; blank lines and # comments are skipped,
    quotes around a value are taken off."""
    settings = {}
    if not path.exists():
        return settings
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        settings[key.strip()] = value.strip().strip('"').strip("'")
    return settings


def settings() -> dict:
    found = {**read_env_file(HERE / ".env"), **os.environ}
    return {
        "host": found.get("BW_HOST", "127.0.0.1"),
        "port": int(found.get("BW_PORT", "8000")),
        "threads": int(found.get("BW_THREADS", "8")),
        "proxy": found.get("BW_PROXY", ""),
    }


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = settings()
    # the navigator's switch (app.py reads it from the environment, or from .env itself)
    found = {**read_env_file(HERE / ".env"), **os.environ}
    if "BW_NAVIGATOR" in found:
        os.environ["BW_NAVIGATOR"] = found["BW_NAVIGATOR"]
    from app import app   # after the logging is set up
    options = {"host": config["host"], "port": config["port"], "threads": config["threads"],
               "ident": "",                      # don't name the server program in the answers
               "channel_timeout": 60}
    if config["proxy"]:
        # behind Caddy: the visitor's address and https come in its headers
        options.update(trusted_proxy=config["proxy"], trusted_proxy_count=1,
                       trusted_proxy_headers={"x-forwarded-for", "x-forwarded-proto", "x-forwarded-host"},
                       clear_untrusted_proxy_headers=True)
    logging.getLogger("bw").info("serving on %s:%s with %s threads", config["host"], config["port"], config["threads"])
    serve(app, **options)


if __name__ == "__main__":
    main()
