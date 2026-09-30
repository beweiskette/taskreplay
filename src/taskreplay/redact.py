"""Keep secrets away from models, logs and result files.

Three helpers:
  * `is_secret_env_name` / `child_env`: drop secret-looking variables from the
    environment handed to a child process.
  * `is_secret_file`: recognise files that usually hold credentials.
  * `redact` / `redact_obj`: replace secret-looking strings and known literal
    secret values with a placeholder.

Pattern matching is a safety net, not a guarantee. See README, Limitations.
"""

from __future__ import annotations

import fnmatch
import os
import re
from pathlib import PurePath
from typing import Any, Iterable, Mapping

PLACEHOLDER = "[REDACTED]"

# -- environment ---------------------------------------------------------------

SECRET_ENV_WORDS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL", "PRIVATE")
SECRET_ENV_NAMES = {
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    "AZURE_OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "HF_TOKEN",
    "HUGGING_FACE_HUB_TOKEN",
    "NPM_TOKEN",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "DATABASE_URL",
}
# Values shorter than this are not redacted literally, to avoid mangling output.
MIN_LITERAL_LEN = 8


def is_secret_env_name(name: str) -> bool:
    upper = name.upper()
    return upper in SECRET_ENV_NAMES or any(word in upper for word in SECRET_ENV_WORDS)


def child_env(base: Mapping[str, str] | None = None, extra_names: Iterable[str] = ()) -> dict[str, str]:
    """A copy of `base` (default os.environ) without secret-looking variables."""
    source = os.environ if base is None else base
    hidden = {n.upper() for n in extra_names if n}
    return {k: v for k, v in source.items() if not is_secret_env_name(k) and k.upper() not in hidden}


def env_secret_values(base: Mapping[str, str] | None = None) -> list[str]:
    """Values of secret-looking variables in the environment, for literal redaction."""
    source = os.environ if base is None else base
    return [v for k, v in source.items() if is_secret_env_name(k) and len(v or "") >= MIN_LITERAL_LEN]


# -- files ---------------------------------------------------------------------

SECRET_FILE_PATTERNS = (
    ".env",
    ".env.*",
    "*.env",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.jks",
    "*.keystore",
    "*.kdbx",
    "id_rsa*",
    "id_dsa*",
    "id_ecdsa*",
    "id_ed25519*",
    ".netrc",
    "_netrc",
    ".git-credentials",
    ".npmrc",
    ".pypirc",
    ".htpasswd",
    "credentials",
    "credentials.json",
    "service-account*.json",
)
# Files matching a secret pattern that are usually safe templates.
SAFE_FILE_PATTERNS = ("*.pub", "*.example", "*.sample", "*.template", "*.dist", ".env.defaults")
SECRET_DIRS = {".ssh", ".gnupg", ".aws", ".docker"}


def is_secret_file(rel_path: str | PurePath) -> bool:
    """True for paths that commonly hold credentials (checked by name, not content)."""
    parts = [p.lower() for p in PurePath(rel_path).parts]
    if not parts:
        return False
    if any(p in SECRET_DIRS for p in parts[:-1]):
        return True
    name = parts[-1]
    if any(fnmatch.fnmatchcase(name, pat) for pat in SAFE_FILE_PATTERNS):
        return False
    return any(fnmatch.fnmatchcase(name, pat) for pat in SECRET_FILE_PATTERNS)


def matches_any(rel_path: str | PurePath, patterns: Iterable[str]) -> bool:
    """Glob match against the full relative path (posix form) or the file name."""
    posix = PurePath(rel_path).as_posix()
    name = PurePath(rel_path).name
    return any(fnmatch.fnmatch(posix, pat) or fnmatch.fnmatch(name, pat) for pat in patterns)


# -- text ----------------------------------------------------------------------

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # PEM private key blocks (also inside JSON strings with escaped newlines)
    (re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----", re.S),
     "[REDACTED PRIVATE KEY]"),
    # Authorization headers and bearer tokens
    (re.compile(r"(?i)(authorization\s*[:=]\s*[\"']?)(basic|token)\s+[A-Za-z0-9._~+/=-]{4,}"),
     r"\1\2 " + PLACEHOLDER),
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 " + PLACEHOLDER),
    # OpenAI / Anthropic style keys: sk-..., sk-proj-..., sk-ant-...
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), PLACEHOLDER),
    # GitHub tokens
    (re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"), PLACEHOLDER),
    # AWS access key ids
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), PLACEHOLDER),
    # Google API keys, Slack tokens, Hugging Face tokens
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), PLACEHOLDER),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), PLACEHOLDER),
    (re.compile(r"\bhf_[A-Za-z0-9]{30,}\b"), PLACEHOLDER),
    # credentials in URLs: scheme://user:password@host
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^\s/:@\"']+:[^\s/@\"']+@"), r"\1" + PLACEHOLDER + "@"),
]


def redact(text: str, literals: Iterable[str] = ()) -> str:
    """Replace secret-looking substrings and the given literal values."""
    if not text:
        return text
    values = sorted({v for v in literals if v and len(v) >= 4}, key=len, reverse=True)
    for value in values:
        text = text.replace(value, PLACEHOLDER)
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    return text


def redact_obj(obj: Any, literals: Iterable[str] = ()) -> Any:
    """`redact` applied to every string inside nested dicts and lists."""
    lits = list(literals)
    if isinstance(obj, str):
        return redact(obj, lits)
    if isinstance(obj, dict):
        return {k: redact_obj(v, lits) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact_obj(v, lits) for v in obj]
    return obj


def default_literals(extra: Iterable[str] = ()) -> list[str]:
    """Literal values to redact: the given ones plus secret-looking environment values."""
    return [v for v in extra if v] + env_secret_values()
