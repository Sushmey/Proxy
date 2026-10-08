"""Reads settings from a .env file at the repo root and from real environment
variables. A real environment variable always wins over the .env file, so
`REMINDER_EMAIL=x python3 email_scheduler.py` still works.

No dependency: .env is simple KEY=VALUE lines, blank lines and # comments
ignored, optional single or double quotes around the value. Copy
.env.example to .env to start; .env is gitignored.
"""

import os

ENV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")


def _load_env_file():
    if not os.path.exists(ENV_FILE):
        return
    with open(ENV_FILE) as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            os.environ.setdefault(key.strip(), value)


_load_env_file()


def get(name, default=None):
    """The setting's value, or `default` if it's unset or blank."""
    value = os.environ.get(name, "").strip()
    return value if value else default
