"""Desktop launcher: serves the FastAPI app and opens it in a pywebview window.

Run with:
    python -m backend.desktop        # from project root
or:
    python desktop.py                 # from the backend/ directory
"""

import argparse
import json
import os
import shutil
import socket
import threading
import time
import uvicorn

import webview

from main import app, jobs

HOST = "127.0.0.1"
DEFAULT_PORT = 8756
WINDOW_TITLE = "YTQuickie v1.0 [Audio Ripper]"
WINDOW_WIDTH = 820
WINDOW_HEIGHT = 620


class JsApi:
    """Exposed to the frontend as window.pywebview.api.*."""

    def minimize(self):
        webview.windows[0].minimize()

    def close(self):
        webview.windows[0].destroy()

    def download_zip(self, job_id: str) -> str:
        """Save the finished archive via a native dialog. Returns a JSON string."""
        job = jobs.get(job_id)
        if not job or not job.get("zip_path"):
            return json.dumps({"ok": False, "error": "Archive not ready yet."})
        src = job["zip_path"]
        if not os.path.exists(src):
            return json.dumps({"ok": False, "error": "Archive file is missing."})

        window = webview.windows[0]
        dest = window.create_file_dialog(
            webview.FileDialog.SAVE,
            save_filename=os.path.basename(src),
            file_types=("Zip archive (*.zip)",),
        )
        if not dest:
            return json.dumps({"ok": False, "cancelled": True})

        destination = dest[0] if isinstance(dest, (tuple, list)) else dest
        try:
            shutil.copyfile(src, destination)
        except OSError as e:
            return json.dumps({"ok": False, "error": f"Could not save file: {e}"})
        return json.dumps({"ok": True, "path": destination})


def _free_port(preferred: int) -> int:
    """Return the preferred port if free, otherwise an OS-assigned free port."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind((HOST, preferred))
        return preferred
    except OSError:
        pass

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((HOST, 0))
        return s.getsockname()[1]


def main() -> None:
    parser = argparse.ArgumentParser(description="Launch YTQuickie desktop app")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"Port to serve the app on (default: {DEFAULT_PORT})")
    parser.add_argument("--debug", action="store_true",
                        help="Keep uvicorn in reload mode and show the dev console")
    args = parser.parse_args()

    port = _free_port(args.port)

    server = uvicorn.Server(
        uvicorn.Config(app, host=HOST, port=port, log_level="warning" if not args.debug else "info")
    )
    threading.Thread(target=server.run, daemon=True).start()

    # Wait until the server is actually accepting connections.
    while not server.started:
        if server.should_exit:
            raise SystemExit("Server failed to start.")
        time.sleep(0.05)

    url = f"http://{HOST}:{port}"

    window = webview.create_window(
        WINDOW_TITLE,
        url,
        width=WINDOW_WIDTH,
        height=WINDOW_HEIGHT,
        min_size=(WINDOW_WIDTH, WINDOW_HEIGHT),
        resizable=True,
        frameless=True,
        easy_drag=False,
        shadow=True,
        background_color="#050507",
        js_api=JsApi(),
    )

    webview.start(debug=args.debug, private_mode=False)

    # Window closed -> shut down the server.
    server.should_exit = True


if __name__ == "__main__":
    main()
