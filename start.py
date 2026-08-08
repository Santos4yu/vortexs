#!/usr/bin/env python3
import subprocess, sys, os

os.chdir("/home/container")

print("[start.py] Running update_board.py...")
subprocess.run([sys.executable, "-u", "backend/update_board.py"], check=False)

print("[start.py] Starting bot/vortex.py...")
os.execv(sys.executable, [sys.executable, "-u", "bot/vortex.py"])
