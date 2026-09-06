"""Headless end-to-end check for the login-free policy/simulation/restore flow."""
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "api"))
    with tempfile.TemporaryDirectory() as directory:
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        origin = f"http://127.0.0.1:{port}"
        os.environ.update(APP_ENV="test", APP_ORIGIN=origin, GOOGLE_API_KEY="",
                          DATABASE_URL="sqlite:///" + (Path(directory) / "browser.db").as_posix())
        import main as api
        import uvicorn
        from playwright.sync_api import sync_playwright

        api.store.initialize()
        server = uvicorn.Server(uvicorn.Config(api.app, log_level="error", access_log=False))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        try:
            deadline = time.time() + 45
            while not server.started:
                if time.time() > deadline:
                    raise RuntimeError("Test server did not start")
                time.sleep(0.05)
            with sync_playwright() as pw:
                edge = Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")
                browser = pw.chromium.launch(headless=True,
                    executable_path=str(edge) if edge.exists() else None)
                page = browser.new_page()
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(origin + "/")
                page.locator("#input").fill("건당 50만원 하루 150만원 신규 수취인은 확인해줘")
                page.locator("#btn-compile").click()
                page.locator("#btn-approve").wait_for(state="visible")
                page.locator("#btn-approve").click()
                page.get_by_text("위임 정책이 적용됐습니다.", exact=True).wait_for()
                page.goto(origin + "/simulate.html")
                page.locator('[data-id="limit_ratcheting"]').click()
                page.locator("#btn-run").click()
                page.wait_for_url("**/result.html?run=*")
                page.on("dialog", lambda dialog: dialog.accept())
                page.locator("#btn-restore").click()
                page.get_by_text("권한을 복원했습니다.", exact=True).wait_for()
                assert not errors, errors
                browser.close()
                print("BROWSER_SMOKE_OK: policy approval, simulation and one-time restore")
        finally:
            server.should_exit = True
            thread.join(timeout=15)
            sock.close()
            api.store.engine.dispose()


if __name__ == "__main__":
    main()
