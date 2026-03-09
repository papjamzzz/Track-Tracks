#!/bin/bash
SCRIPT=~/track_cpu_monitor/viewer/main.py

arch -arm64 /usr/bin/python3 "$SCRIPT" 2>/tmp/tracktracks_error.log
if [ $? -ne 0 ]; then
    osascript -e "display dialog \"TrackTracks failed to start:\" & return & (do shell script \"cat /tmp/tracktracks_error.log | tail -5\") buttons {\"OK\"} with icon stop"
fi
