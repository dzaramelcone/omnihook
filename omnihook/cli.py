"""omnihook CLI — thin wrapper over the HTTP API.

Usage:
    omnihook status                  Show global state + sessions
    omnihook health                  Health check
    omnihook enable [SESSION]        Enable globally or for a session
    omnihook disable [SESSION]       Disable globally or for a session
    omnihook machine                 Show state machine + lifecycle + registry
    omnihook machine reset           Reset machine to defaults
    omnihook machine reload          Hot-reload handlers.py
    omnihook lifecycle               Show lifecycle hooks
    omnihook handlers                List handler functions with source
    omnihook rate-limit MAX WINDOW   Set rate limit (calls per window_sec)
"""

import json
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:9100"


def _req(method: str, path: str, body: dict | None = None) -> dict | str:
    data = json.dumps(body).encode() if body else None
    headers = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(
        f"{BASE}{path}", data=data, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.URLError:
        print(
            f"error: cannot connect to omnihook at {BASE}\n"
            "Server not running. Start it with: omnihook-server\n"
            "Or run the quickstart: curl -fsSL https://raw.githubusercontent.com/dzaramelcone/omnihook/main/quickstart.sh | bash",
            file=sys.stderr,
        )
        sys.exit(1)


def _get(path: str) -> dict:
    return _req("GET", path)


def _post(path: str, body: dict | None = None) -> dict:
    return _req("POST", path, body)


def _pp(data):
    print(json.dumps(data, indent=2))


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help", "help"):
        print(__doc__.strip())
        return

    cmd = args[0]

    if cmd == "health":
        _pp(_get("/health"))

    elif cmd == "status":
        _pp(_get("/ctl/status"))

    elif cmd == "enable":
        if len(args) > 1:
            _pp(_post(f"/ctl/enable/{args[1]}"))
        else:
            _pp(_post("/ctl/enable"))

    elif cmd == "disable":
        if len(args) > 1:
            _pp(_post(f"/ctl/disable/{args[1]}"))
        else:
            _pp(_post("/ctl/disable"))

    elif cmd == "machine":
        if len(args) > 1 and args[1] == "reset":
            _pp(_post("/ctl/machine/reset"))
        elif len(args) > 1 and args[1] == "reload":
            _pp(_post("/ctl/machine/reload"))
        else:
            _pp(_get("/ctl/machine"))

    elif cmd == "lifecycle":
        _pp(_get("/ctl/lifecycle"))

    elif cmd == "handlers":
        _pp(_get("/handlers"))

    elif cmd == "rate-limit":
        if len(args) < 3:
            print("usage: omnihook rate-limit MAX_CALLS WINDOW_SEC")
            sys.exit(1)
        _pp(
            _post(
                "/ctl/rate-limit",
                {"max_calls": int(args[1]), "window_sec": float(args[2])},
            )
        )

    else:
        print(f"unknown command: {cmd}")
        print(__doc__.strip())
        sys.exit(1)


if __name__ == "__main__":
    main()
