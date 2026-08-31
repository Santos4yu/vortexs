#!/usr/bin/env python3
"""Compatibility entry point for hosts configured with STARTUP_FILE=startup.py."""

import os
import sys


PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
BOT_ENTRYPOINT = os.path.join(PROJECT_ROOT, "bot", "vortex.py")

os.chdir(PROJECT_ROOT)
print("[startup.py] Starting VORTEX; board refresh runs in the background...")
os.execv(sys.executable, [sys.executable, "-u", BOT_ENTRYPOINT])
