#!/usr/bin/env python3
"""End-to-end check of the three console fixes.

1. The conversation is still there after the host restarts.
2. A console that was open across the restart catches up by itself.
3. The polling keeps working, so new lines show up without a reload.

The console's own JavaScript is exercised through the same endpoints the
page uses, and the page's source is checked for the behaviour that
cannot be seen from the outside (the instance check).
"""
import json
import sys
import time
import urllib.request
from pathlib import Path

HOST_DIR = Path(__file__).resolve().parents[1] / "host"
sys.path.insert(0, str(HOST_DIR))

import config as config_module  # noqa: E402
from engine import Engine  # noqa: E402

PORT = 8799
# The second host listens on its own port: the first one still holds
# the socket for a moment after it was told to stop.
PORT2 = 8798
TOKEN = "e2e-token"
failures = []


def check(name, condition, detail=""):
    print(("  ok   " if condition else "  FAIL ") + name +
          ((" -- " + detail) if detail and not condition else ""))
    if not condition:
        failures.append(name)


def get(path, token=TOKEN, port=PORT):
    request = urllib.request.Request(
        "http://127.0.0.1:%d%s" % (port, path),
        headers={"Authorization": "Bearer " + token})
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def main():
    import tempfile
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    state = root / "state.json"
    config = config_module.create_config(root / "config.json")
    config["_root"] = str(root)
    config["_path"] = str(root / "config.json")

    from http.server import ThreadingHTTPServer
    import server as server_module

    class Server(ThreadingHTTPServer):
        # The host is restarted on the same port, so the socket has to
        # be allowed to be reused right away.
        allow_reuse_address = True

    engine = Engine(config, state_file=str(state))
    handler = server_module.make_handler(engine, TOKEN)
    httpd = Server(("127.0.0.1", PORT), handler)
    httpd.daemon_threads = True

    import threading
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.3)

    try:
        print("1. the conversation is written down")
        engine.log_transcript("user", "direct", "你好")
        engine.log_transcript("agent", "direct", "你好呀")
        time.sleep(1.5)          # the writer wakes up once a second
        data = get("/api/threads")
        check("the conversation is visible",
              any("你好呀" in row["message"]
                  for rows in data["threads"].values() for row in rows))

        print("2. the host restarts")
        first_instance = data["instance_id"]
        last_seq = data["last_seq"]
        httpd.shutdown()
        engine.close()
        time.sleep(0.3)

        engine2 = Engine(config, state_file=str(state))
        handler2 = server_module.make_handler(engine2, TOKEN)
        httpd2 = Server(("127.0.0.1", PORT2), handler2)
        httpd2.daemon_threads = True
        threading.Thread(target=httpd2.serve_forever, daemon=True).start()
        time.sleep(0.3)

        data = get("/api/threads", port=PORT2)
        messages = [row["message"]
                    for rows in data["threads"].values() for row in rows]
        check("the conversation survived the restart",
              "你好呀" in messages, str(messages))
        check("the host has a new identity",
              data["instance_id"] != first_instance)
        check("the numbers keep growing",
              data["last_seq"] >= last_seq,
              "%s < %s" % (data["last_seq"], last_seq))

        print("3. a console that was open across the restart")
        # The page kept last_seq from before the restart and the old
        # identity. Asking for "everything after last_seq" against the
        # new host must not be the end of the story: the page notices
        # the new identity and redraws.
        old = get("/api/transcript?after=%d" % last_seq, port=PORT2)
        check("the new identity is handed out",
              old.get("instance_id") == data["instance_id"])
        check("the number cursor is still usable",
              isinstance(old.get("last_seq"), int))

        print("4. the polling keeps working")
        engine2.log_transcript("user", "direct", "新的一句")
        time.sleep(1.5)
        fresh = get("/api/transcript?after=%d" % last_seq, port=PORT2)
        check("a new line shows up without a reload",
              any("新的一句" in row["message"] for row in fresh["items"]),
              str(fresh["items"]))

        print("5. the page source")
        page = (HOST_DIR / "web" / "console.html").read_text(encoding="utf-8")
        check("the page knows about the instance", "instance_id" in page)
        check("the page redraws on a new instance",
              "hostInstance !== data.instance_id" in page)
        check("the page clears the old cursor", "function resetThreads" in page)
        check("the send button is a plain arrow",
              "M21.5 2.5 11 13" in page)

        httpd2.shutdown()
        engine2.close()
    finally:
        tmp.cleanup()

    print()
    if failures:
        print("FAILED: " + ", ".join(failures))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
