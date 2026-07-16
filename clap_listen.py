"""
clap_listen.py -- Optional double-clap wake trigger for Jeeves.

Runs entirely locally via sounddevice; no cloud calls, no extra cost.
Listens for two sharp volume spikes (claps) within a tight time window
and fires a callback -- useful as a hands-free alternative to a wake
word, e.g. to toggle Jeeves' mute state without saying anything.

This module does nothing unless something explicitly starts it (see
start_clap_listener below), so importing it has zero effect on the
rest of Jeeves' behavior.

Tuning here follows the same proven shape as other clap-detection
scripts in the wild (tight double-clap gap, retrigger hysteresis,
a quiet-gated noise floor, and mic auto-fallback), rather than a
looser first pass -- these details are what separate "works most of
the time" from "rarely false-triggers, rarely misses a clap".
"""

import threading
import time

import numpy as np
import sounddevice as sd

# --- Tuning -----------------------------------------------------------------
SAMPLE_RATE = 44100
BLOCK_MS = 40                  # analysis window; smaller = snappier, noisier
CHANNELS = 1

SPIKE_RATIO = 7.0              # how many times louder than the noise floor = a clap
MIN_RMS = 0.012                # ignore spikes below this absolute level
COOLDOWN_S = 0.45              # minimum gap between two logged claps
MIN_DOUBLE_GAP_S = 0.05        # claps closer than this are the same clap's echo
MAX_DOUBLE_GAP_S = 0.35        # claps further apart than this are two separate attempts
RETRIGGER_RATIO = 0.55         # audio must fall below threshold * this before a new hit counts
NOISE_FLOOR_ALPHA = 0.992      # closer to 1 = slower adaptation to room noise
QUIET_GATE_MULT = 2.2          # only update noise floor when below floor * this (loud claps don't corrupt it)

INPUT_PROBE_S = 0.5            # startup mic probe duration
INPUT_SILENT_RMS = 0.001       # below this, the default mic is considered silent
# -----------------------------------------------------------------------------

_block_size = int(SAMPLE_RATE * BLOCK_MS / 1000)


def _rms(block: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.square(block.astype(np.float32)))))


def _pick_input_device():
    """
    Probes the default input device briefly; if it's effectively silent,
    scans all input devices and picks the loudest one that actually
    produces signal. Returns a device index or None (use system default).
    """
    try:
        default_idx = sd.default.device[0]
        with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32") as stream:
            frames, _ = stream.read(int(SAMPLE_RATE * INPUT_PROBE_S))
        if _rms(frames) > INPUT_SILENT_RMS:
            return default_idx  # default mic works fine
    except Exception:
        pass

    # Default mic is silent or unavailable -- scan for a louder alternative.
    best_idx, best_level = None, 0.0
    try:
        for idx, dev in enumerate(sd.query_devices()):
            if dev.get("max_input_channels", 0) <= 0:
                continue
            try:
                with sd.InputStream(samplerate=SAMPLE_RATE, channels=1,
                                     dtype="float32", device=idx) as stream:
                    frames, _ = stream.read(int(SAMPLE_RATE * 0.3))
                level = _rms(frames)
                if level > best_level:
                    best_level, best_idx = level, idx
            except Exception:
                continue
    except Exception:
        pass
    return best_idx


def start_clap_listener(on_double_clap, device=None):
    """
    Starts a background thread that listens for a double-clap and calls
    on_double_clap() (no arguments) each time one is detected.

    Safe to call once at startup; does nothing until then. If `device`
    is not given, auto-probes for a working microphone (falling back to
    the loudest available input if the system default is silent).
    Returns the background thread (daemon=True).
    """

    def _loop():
        chosen_device = device if device is not None else _pick_input_device()

        background_level = MIN_RMS
        last_clap_time = 0.0
        pending_first_clap = None
        armed = True  # False while we're waiting for the level to drop (retrigger gate)

        def callback(indata, frames, time_info, status):
            nonlocal background_level, last_clap_time, pending_first_clap, armed

            level = _rms(indata[:, 0] if indata.ndim > 1 else indata)
            now = time.time()
            threshold = background_level * SPIKE_RATIO

            # Only adapt the noise floor when things are quiet -- a clap
            # itself must never drag the baseline upward.
            if level < background_level * QUIET_GATE_MULT:
                background_level = NOISE_FLOOR_ALPHA * background_level + (1 - NOISE_FLOOR_ALPHA) * level

            if not armed:
                if level < threshold * RETRIGGER_RATIO:
                    armed = True
                return

            is_spike = (
                level > MIN_RMS
                and level > threshold
                and (now - last_clap_time) > COOLDOWN_S
            )
            if not is_spike:
                return

            armed = False  # require the level to fall before counting another hit
            last_clap_time = now

            if pending_first_clap is None:
                pending_first_clap = now
                return

            gap = now - pending_first_clap
            if MIN_DOUBLE_GAP_S <= gap <= MAX_DOUBLE_GAP_S:
                pending_first_clap = None
                try:
                    on_double_clap()
                except Exception as e:
                    print(f"[ClapListen] on_double_clap callback error: {e}")
            elif gap > MAX_DOUBLE_GAP_S:
                pending_first_clap = now  # too slow -- treat this as a fresh first clap
            # if gap < MIN_DOUBLE_GAP_S, ignore it as the same clap's echo

        try:
            with sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=CHANNELS,
                dtype="float32",
                blocksize=_block_size,
                device=chosen_device,
                callback=callback,
            ):
                print(f"[ClapListen] Listening for double-claps (device={chosen_device})...")
                while True:
                    time.sleep(0.2)
        except Exception as e:
            print(f"[ClapListen] Could not start (mic busy or unavailable?): {e}")

    thread = threading.Thread(target=_loop, daemon=True, name="ClapListenThread")
    thread.start()
    return thread


if __name__ == "__main__":
    def _demo():
        print("Double-clap detected!")
    start_clap_listener(_demo)
    print("Clap twice near your mic to test. Ctrl+C to quit.")
    while True:
        time.sleep(1)
