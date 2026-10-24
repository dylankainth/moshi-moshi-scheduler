import threading
import time
from dataclasses import dataclass

from .privacy import IgnoreList
from .source import UrlReader, WindowSource, url_from_title

POLL_S = 3.0


@dataclass(frozen=True)
class Sample:
    ts: float
    kind: str  # window | nowindow | private
    exe: str | None
    title: str | None
    url: str | None
    category: str | None
    idle_s: float


def take_sample(source: WindowSource, ignore: IgnoreList, now: float, url_reader: UrlReader = url_from_title) -> Sample:
    idle = source.idle_seconds()
    w = source.foreground()
    if w is None:
        return Sample(now, "nowindow", None, None, None, None, idle)
    cat = ignore.category(w)
    if cat:
        # Redacted before anything is stored or passed on: category only.
        return Sample(now, "private", None, None, None, cat, idle)
    return Sample(now, "window", w.exe, w.title, url_reader(w), None, idle)


def write(conn, s: Sample):
    conn.execute("INSERT INTO samples(ts, kind, exe, title, url, category, idle_s) VALUES (?,?,?,?,?,?,?)",
                 (s.ts, s.kind, s.exe, s.title, s.url, s.category, s.idle_s))
    conn.commit()


def run(conn, source: WindowSource, ignore: IgnoreList, stop: threading.Event, interval=POLL_S, on_sample=None):
    # Sleep/hibernate simply produces no rows; downstream treats gaps as no data.
    while not stop.is_set():
        s = take_sample(source, ignore, time.time())
        write(conn, s)
        if on_sample:
            on_sample(s)
        stop.wait(interval)
