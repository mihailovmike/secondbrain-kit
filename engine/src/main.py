"""Main entrypoint: runs watcher + FastAPI server in parallel."""

import threading
import uvicorn

from .watcher import start_watcher


def run_api():
    uvicorn.run("src.api:app", host="0.0.0.0", port=8789, log_level="info")


def main():
    # Start API server in a thread
    api_thread = threading.Thread(target=run_api, daemon=True)
    api_thread.start()

    # Watcher runs in main thread (blocking)
    start_watcher()


if __name__ == "__main__":
    main()
