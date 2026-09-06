import os
import uvicorn
import threading
import time
import webbrowser

from medik_pilot.app import app


if __name__ == "__main__":
    host = os.getenv("MEDIKTEST_HOST", "127.0.0.1")
    port = int(os.getenv("MEDIKTEST_PORT", "8765"))
    no_browser = os.getenv("MEDIKTEST_NO_BROWSER", "").strip().lower()
    if no_browser not in {"1", "true", "yes", "on"}:
        threading.Thread(
            target=lambda: (time.sleep(1.2), webbrowser.open("http://{}:{}".format(host, port))),
            daemon=True,
        ).start()
    uvicorn.run(app, host=host, port=port, reload=False)
