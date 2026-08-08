#!/usr/bin/env python3
import sys, os

os.chdir("/home/container")

print("[start.py] Starting bot/vortex.py...")
os.execv(sys.executable, [sys.executable, "-u", "bot/vortex.py"])
