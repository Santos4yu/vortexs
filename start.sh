#!/bin/bash
# Resilient startup — auto-restarts vortex.py if it crashes

if [[ -d .git ]] && [[ "0" == "1" ]]; then git pull; fi
if [[ ! -z "" ]]; then pip install -U --prefix .local ; fi
if [[ -f /home/container/${REQUIREMENTS_FILE} ]]; then pip install -U --prefix .local -r ${REQUIREMENTS_FILE}; fi

# Run board update once
/usr/local/bin/python /home/container/backend/update_board.py

# Auto-restart bot if it crashes
while true; do
    echo "[$(date)] Starting vortex.py..."
    /usr/local/bin/python /home/container/bot/vortex.py
    echo "[$(date)] vortex.py exited (code $?). Restarting in 10s..."
    sleep 10
done
