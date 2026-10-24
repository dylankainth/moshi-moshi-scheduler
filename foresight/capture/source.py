import re
from dataclasses import dataclass
from typing import Callable, Protocol


@dataclass(frozen=True)
class RawWindow:
    exe: str | None  # e.g. "code.exe"; None if the process couldn't be opened (elevated)
    title: str


class WindowSource(Protocol):
    def foreground(self) -> RawWindow | None: ...  # None = locked / secure desktop
    def idle_seconds(self) -> float: ...


BROWSERS = {"chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe", "arc.exe", "vivaldi.exe"}
_URL = re.compile(r"https?://\S+|\b(?:[a-z0-9-]+\.)+(?:com|org|net|io|dev|ai|app|co|uk|edu|gov)\b(?:/\S*)?", re.I)


def url_from_title(w: RawWindow) -> str | None:
    # Most browsers don't put the URL in the title; this catches the cases that do (localhost, bare domains).
    if (w.exe or "").lower() not in BROWSERS:
        return None
    m = _URL.search(w.title)
    return m.group(0) if m else None


# Hook for a proper URL reader (UI Automation, browser extension) later.
UrlReader = Callable[[RawWindow], str | None]
