"""Creates the folders the program writes into.

state/ and credentials/ are gitignored, so a fresh clone has neither, and
several writers (the tool-call log, the Telegram offset file, the profile
files) open files inside them without creating the folder first. Each entry
point calls ensure_dirs() once at startup. Paths are relative to the working
directory, like every other path in this repo, so run from the repo root.
"""

import os

DIRS = (
    "state/telegram/conversations",
    "state/email",
    "state/shopping/pending",
    "state/shopping/debug",
    "state/shopping/login_debug",
    "credentials/backend",
    "credentials/users/owner",
)


def ensure_dirs():
    """Create any missing folders. Safe to call repeatedly."""
    for path in DIRS:
        os.makedirs(path, exist_ok=True)
