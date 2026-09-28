from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

_SECRET_PATTERNS = [
    re.compile(r"whsec_[A-Za-z0-9_\-+=/]+"),
    re.compile(r"(?i)(recall[_-]?api[_-]?key\s*[:=]\s*)[^\s,;]+"),
    re.compile(r"(?i)(passcode|pwd)=([^&\s]+)"),
]


def redact_text(text: str) -> str:
    out = str(text or "")
    for pat in _SECRET_PATTERNS:
        if pat.pattern.startswith("(?i)(passcode"):
            out = pat.sub(lambda m: f"{m.group(1)}=<redacted>", out)
        elif pat.pattern.startswith("(?i)(recall"):
            out = pat.sub(lambda m: f"{m.group(1)}<redacted>", out)
        else:
            out = pat.sub("whsec_<redacted>", out)
    return out


def redact_meeting_url(raw: str) -> str:
    try:
        p = urlsplit(str(raw or ""))
        if not p.scheme or not p.netloc:
            return redact_text(raw)
        return urlunsplit((p.scheme, p.netloc, p.path, "", ""))
    except Exception:
        return redact_text(raw)
