"""
TrackCpuMonitor — Ableton Live 12 Remote Script  (Enhanced)

Streams per-device cpu_load, track metadata, and song info
to the viewer app via UDP/JSON every 200 ms.
"""

import socket
import json

UDP_HOST = "127.0.0.1"
UDP_PORT = 7400
POLL_MS  = 200


def create_instance(c_instance):
    return TrackCpuMonitor(c_instance)


class TrackCpuMonitor:
    def __init__(self, c_instance):
        self._c    = c_instance
        self._song = c_instance.song()
        self._active = True

        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.setblocking(False)
        except Exception:
            self._sock = None

        self._schedule()

    # ── scheduling ────────────────────────────────────────────────────────────
    def _schedule(self):
        if self._active:
            self._c.schedule_message(POLL_MS, self._poll)

    def _poll(self):
        if not self._active:
            return
        try:
            self._send()
        except Exception:
            pass
        self._schedule()

    # ── helpers ───────────────────────────────────────────────────────────────
    def _track_type(self, track):
        try:
            if getattr(track, "is_foldable", False):
                return "group"
            if getattr(track, "has_midi_input", False):
                return "midi"
        except Exception:
            pass
        return "audio"

    def _track_color(self, track):
        try:
            c = int(track.color)
            return [(c >> 16) & 0xFF, (c >> 8) & 0xFF, c & 0xFF]
        except Exception:
            return [80, 80, 80]

    def _collect_devices(self, track):
        devices_out = []
        total_cpu   = 0.0
        for device in track.devices:
            try:
                pct = round(float(device.cpu_load) * 100.0, 2)
            except AttributeError:
                pct = 0.0
            total_cpu += pct
            try:
                cls = str(device.class_name)
            except AttributeError:
                cls = device.name
            devices_out.append({
                "name":   device.name,
                "class":  cls,
                "cpu":    pct,
                "active": bool(device.is_active),
            })
        return round(total_cpu, 2), devices_out

    def _serialize_track(self, track, is_return=False):
        cpu, devices = self._collect_devices(track)
        return {
            "name":    track.name,
            "type":    "return" if is_return else self._track_type(track),
            "color":   self._track_color(track),
            "muted":   bool(track.mute),
            "solo":    bool(getattr(track, "solo", False)),
            "cpu":     cpu,
            "devices": devices,
        }

    # ── main send ─────────────────────────────────────────────────────────────
    def _send(self):
        if self._sock is None:
            return

        # song metadata
        try:
            bpm = round(float(self._song.tempo), 2)
        except Exception:
            bpm = 0.0
        try:
            playing = bool(self._song.is_playing)
        except Exception:
            playing = False
        try:
            sig_n = int(self._song.signature_numerator)
            sig_d = int(self._song.signature_denominator)
        except Exception:
            sig_n, sig_d = 4, 4

        # master track CPU
        master_cpu = 0.0
        try:
            for dev in self._song.master_track.devices:
                try:
                    master_cpu += float(dev.cpu_load) * 100.0
                except AttributeError:
                    pass
        except Exception:
            pass

        payload = json.dumps({
            "meta": {
                "bpm":        bpm,
                "playing":    playing,
                "sig_num":    sig_n,
                "sig_den":    sig_d,
                "master_cpu": round(master_cpu, 2),
            },
            "tracks":  [self._serialize_track(t)               for t in self._song.tracks],
            "returns": [self._serialize_track(t, is_return=True) for t in self._song.return_tracks],
        }).encode("utf-8")

        try:
            self._sock.sendto(payload, (UDP_HOST, UDP_PORT))
        except (BlockingIOError, OSError):
            pass

    # ── lifecycle ─────────────────────────────────────────────────────────────
    def disconnect(self):
        self._active = False
        try:
            self._sock.close()
        except Exception:
            pass
