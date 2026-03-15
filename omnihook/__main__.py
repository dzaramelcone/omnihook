import atexit
import logging

import uvicorn

from .store import clear_pid, write_pid


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    write_pid()
    atexit.register(clear_pid)
    uvicorn.run("omnihook.app:app", host="127.0.0.1", port=9100, log_level="warning")


if __name__ == "__main__":
    main()
