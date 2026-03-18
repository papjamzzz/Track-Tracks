.PHONY: setup run

setup:
	pip3 install -r viewer/requirements.txt
	@echo "✅ TrackTracks ready — run: make run"

run:
	arch -arm64 python3 viewer/main.py
