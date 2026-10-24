import json
import re
from pathlib import Path

from .source import RawWindow

# Category -> exe names and whole-word title keywords. Title matches over-redact by design (safe direction).
DEFAULT_IGNORE = {
    "private_browsing": {"exe": [], "title": ["inprivate", "incognito", "private browsing"]},
    "password_manager": {"exe": ["1password.exe", "keepass.exe", "keepassxc.exe", "bitwarden.exe", "dashlane.exe"],
                         "title": ["1password", "bitwarden", "lastpass", "keepass", "keepassxc", "dashlane"]},
    "messaging": {"exe": ["slack.exe", "discord.exe", "teams.exe", "ms-teams.exe", "whatsapp.exe",
                          "telegram.exe", "signal.exe", "messenger.exe"],
                  "title": ["slack", "whatsapp", "messenger", "discord", "telegram", "microsoft teams"]},
    "email": {"exe": ["outlook.exe", "olk.exe", "hxoutlook.exe", "thunderbird.exe"],
              "title": ["gmail", "outlook", "inbox", "proton mail", "thunderbird"]},
    "banking": {"exe": [], "title": ["bank", "banking", "paypal", "monzo", "revolut", "barclays", "hsbc", "natwest",
                                     "lloyds", "santander", "starling", "chase", "wise"]},
}


class IgnoreList:
    def __init__(self, rules: dict):
        self.rules = rules
        self._compiled = [
            (cat, {e.lower() for e in r.get("exe", [])},
             re.compile(r"\b(?:" + "|".join(map(re.escape, r["title"])) + r")\b", re.I) if r.get("title") else None)
            for cat, r in rules.items()
        ]

    def category(self, w: RawWindow) -> str | None:
        exe = (w.exe or "").lower()
        for cat, exes, title_re in self._compiled:
            if exe in exes or (title_re and title_re.search(w.title)):
                return cat
        return None

    @classmethod
    def load(cls, path: Path) -> "IgnoreList":
        if not path.exists():
            path.write_text(json.dumps(DEFAULT_IGNORE, indent=2))
        return cls(json.loads(path.read_text()))
