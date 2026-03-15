import atexit
import logging
import os

import uvicorn

from .store import clear_pid, write_pid


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    write_pid()
    atexit.register(clear_pid)
    host = os.environ.get("OMNIHOOK_HOST", "127.0.0.1")
    port = int(os.environ.get("OMNIHOOK_PORT", "9100"))
    uvicorn.run("omnihook.app:app", host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
