import argparse
import os
import sys
import threading
import time
import webbrowser

from . import db
from .capture.privacy import IgnoreList
from .capture.recorder import POLL_S, Sample, take_sample


def fmt(s: Sample) -> str:
    t = time.strftime("%H:%M:%S", time.localtime(s.ts))
    if s.kind == "nowindow":
        body = "[no window: locked / secure desktop]"
    elif s.kind == "private":
        body = f"[private: {s.category}]"
    else:
        body = f"{s.exe} | {s.title}" + (f" | {s.url}" if s.url else "")
    return f"{t} idle={s.idle_s:5.0f}s  {body}"


def _start_agent(hub, stop):
    from .agent import Agent, run_attribution, run_capture
    from .capture.win32 import Win32Source
    threading.Thread(target=run_capture, args=(db.connect(), Win32Source(), stop), daemon=True).start()
    threading.Thread(target=run_attribution, args=(Agent(db.connect(), hub), stop), daemon=True).start()


def _wait(stop):
    try:
        while not stop.wait(1):
            pass
    except KeyboardInterrupt:
        stop.set()


def load_env(path=".env"):
    """KEY=value lines from ./.env; real environment variables win."""
    try:
        lines = open(path, encoding="utf-8").read().splitlines()
    except FileNotFoundError:
        return
    for line in lines:
        k, sep, v = line.strip().partition("=")
        if sep and k and not k.startswith("#"):
            os.environ.setdefault(k.strip(), v.split(" #")[0].strip().strip("'\""))


def main(argv=None):
    load_env()
    sys.stdout.reconfigure(errors="replace")  # window titles can hold glyphs the Windows console can't print
    p = argparse.ArgumentParser(prog="foresight")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="print what would be captured for 30 s (nothing is written)")
    c.add_argument("--seconds", type=float, default=30)
    r = sub.add_parser("run", help="local mode: API + UI on loopback, capture agent, nothing leaves this machine")
    r.add_argument("--port", type=int, default=8765)
    r.add_argument("--inference", choices=["none", "ollama"], default="none")
    r.add_argument("--no-capture", action="store_true", help="UI only (demo replay still works)")
    r.add_argument("--no-browser", action="store_true")
    s = sub.add_parser("serve", help="cloud mode server (runs on the Vultr VM)")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--inference", choices=["none", "vultr", "openai"],
                   default=os.environ.get("FORESIGHT_INFERENCE") or ("openai" if os.environ.get("OPENAI_API_KEY") else "vultr"))
    a = sub.add_parser("agent", help="cloud mode agent on your PC: captures locally, sends only attributed segments")
    a.add_argument("--server", required=True)
    a.add_argument("--token", default=os.environ.get("FORESIGHT_TOKEN"))
    args = p.parse_args(argv)
    stop = threading.Event()

    if args.cmd == "check":
        from .capture.win32 import Win32Source
        src, ignore = Win32Source(), IgnoreList.load(db.data_dir() / "ignore.json")
        end = time.time() + args.seconds
        while time.time() < end:
            print(fmt(take_sample(src, ignore, time.time())), flush=True)
            time.sleep(POLL_S)
        return

    if args.cmd == "agent":
        from .hub import HttpHub
        if not args.token:
            raise SystemExit("set FORESIGHT_TOKEN or pass --token")
        _start_agent(HttpHub(args.server, args.token), stop)
        print(f"agent running; sending attributed segments only to {args.server} (Ctrl+C to stop)")
        return _wait(stop)

    from .app import App
    from .inference import Provider, make_provider
    from .server import serve
    mode = "local" if args.cmd == "run" else "cloud"
    try:
        provider = make_provider(mode, args.inference)
    except ValueError as e:  # e.g. no API key: serve with rules + template only
        print(f"inference disabled: {e}")
        provider = Provider()
    if provider.name in ("vultr", "openai"):
        try:
            print(f"{provider.name} model:", provider.pick())
        except Exception as e:  # keep serving; judge/explain fall back to rules/template
            print(f"could not list {provider.name} models:", type(e).__name__)
    path = db.data_dir() / "foresight.db"
    app = App(db.connect(path), mode=mode, provider=provider, db_path=path,
              demo_speed=float(os.environ.get("FORESIGHT_DEMO_SPEED", 30)))
    host = "127.0.0.1" if mode == "local" else args.host
    token = os.environ.get("FORESIGHT_TOKEN") if mode == "cloud" else None
    if mode == "cloud" and not token:
        print("FORESIGHT_TOKEN not set: owner endpoints are disabled; demo sessions only")
    httpd = serve(app, host, args.port, token)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{args.port}/"
    print(f"Foresight ({mode}) on {url if mode == 'local' else f'{host}:{args.port}'}")
    if mode == "local":
        if not args.no_capture:
            from .hub import LocalHub
            _start_agent(LocalHub(app), stop)
        if not args.no_browser:
            webbrowser.open(url)
    _wait(stop)
    httpd.shutdown()


if __name__ == "__main__":
    main()
