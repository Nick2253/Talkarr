#!/usr/bin/env python3
"""Run a command in the talkarr-lab container on Steel via Komodo.

    scripts/lab.py python -m talkarr.eval run --corpus eval/corpus.yaml
    scripts/lab.py --no-pull ls /media/movies

Uses Komodo's RunStackService (``docker compose run``) on the seastone-lab stack,
waits for it to finish, and prints the container output. Credentials come from
/home/coder/projects/.env (KOMODO_ADDRESS, KOMODO_API_KEY, KOMODO_API_SECRET).
Exit status is 0 only if Komodo reports success.
"""

import argparse
import json
import sys
import time
import urllib.request

ENV_FILE = "/home/coder/projects/.env"
STACK = "seastone-lab"
SERVICE = "talkarr-lab"


def load_env(path=ENV_FILE):
    env = {}
    with open(path) as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip().strip("\"'")
    return env


class Komodo:
    def __init__(self, env):
        self.address = env["KOMODO_ADDRESS"].rstrip("/")
        self.headers = {
            "Content-Type": "application/json",
            "X-Api-Key": env["KOMODO_API_KEY"],
            "X-Api-Secret": env["KOMODO_API_SECRET"],
        }

    def call(self, kind, request_type, params):
        body = json.dumps({"type": request_type, "params": params}).encode()
        req = urllib.request.Request(f"{self.address}/{kind}", body, self.headers)
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.load(resp)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--no-pull", action="store_true", help="don't pull the :lab image first")
    parser.add_argument("--timeout", type=int, default=6 * 3600, help="seconds to wait")
    parser.add_argument("-e", "--env", action="append", default=[], help="KEY=VALUE for the run")
    parser.add_argument("--user", help="run as this user (e.g. 0 for one-off maintenance)")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    komodo = Komodo(load_env())
    params = {
        "stack": STACK,
        "service": SERVICE,
        "no_tty": True,
        "pull": not args.no_pull,
        "env": dict(item.split("=", 1) for item in args.env),
    }
    if args.command:
        params["command"] = args.command
    if args.user:
        params["user"] = args.user

    update = komodo.call("execute", "RunStackService", params)
    update_id = update.get("_id", {}).get("$oid")
    if not update_id:
        print(json.dumps(update, indent=2), file=sys.stderr)
        return 2

    deadline = time.monotonic() + args.timeout
    while update.get("status") != "Complete":
        if time.monotonic() > deadline:
            print(f"timed out waiting for update {update_id}", file=sys.stderr)
            return 3
        time.sleep(5)
        update = komodo.call("read", "GetUpdate", {"id": update_id})

    for entry in update.get("logs", []):
        stage = entry.get("stage", "")
        out, err = entry.get("stdout", ""), entry.get("stderr", "")
        print(f"--- {stage} ({'ok' if entry.get('success') else 'FAILED'})", file=sys.stderr)
        if out:
            print(out)
        if err:
            print(err, file=sys.stderr)

    return 0 if update.get("success") else 1


if __name__ == "__main__":
    sys.exit(main())
