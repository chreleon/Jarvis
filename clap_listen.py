"""
clap_listen.py -- Optional double-clap wake trigger for Jeeves.

Runs entirely locally via sounddevice; no cloud calls, no extra cost.
Listens for two sharp volume spikes (claps) within a short window and
fires a callback -- useful as a hands-free alternative to a wake word,
e.g. to toggle Jeeves' mute state without saying anything.

This module does nothing unless something explicitly starts it (see
start_clap_listener below), so importing it has zero effect on the
rest of Jeeves' behavior.

Tuning constants below are deliberately exposed at the top, same
spirit as similar clap-detection scripts -- adjust if your mic/room
gives false triggers or misses claps.
"""

import threading
import time

import numpy as np
import sounddevice as sd

# --- Tuning ---------------------------------------------------------------
SAMPLE_RATE = 44100
BLOCK_MS = 30                 # audio block size in ms
SPIKE_RATIO = 3.0             # how much louder than background a clap must be
MIN_RMS = 0.02                # floor so silence doesn't count as a "spike"
COOLDOWN_S = 0.35             # minimum gap between two claps counted together
DOUBLE_CLAP_WINDOW_S = 1.2    # max time between clap 1 and clap 2
# ---------------------------------------------------------------------------

_block_size = int(SAMPLE_RATE * BLOCK_MS / 1000)


def _rms(block: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.square(block.astype(np.float32)))))


def start_clap_listener(on_double_clap, device=None):
    """
    Starts a background thread that listens for a double-clap and calls
    on_double_clap() (no arguments) each time one is detected.

    Safe to call once at startup; does nothing until then. Returns the
    background thread (daemon=True), in case the caller wants to track it.
    """

    def _loop():
        background_level = MIN_RMS
        last_clap_time = 0.0
        pending_first_clap = None

        def callback(indata, frames, time_info, status):
            nonlocal background_level, last_clap_time, pending_first_clap

            level = _rms(indata[:, 0] if indata.ndim > 1 else indata)
            now = time.time()

            # Slowly adapt to ambient noise so a noisy room doesn't cause
            # constant false triggers.
            background_level = 0.98 * background_level + 0.02 * level

            is_spike = (
                level > MIN_RMS
                and level > background_level * SPIKE_RATIO
                and (now - last_clap_time) > COOLDOWN_S
            )
            if not is_spike:
                return

            last_clap_time = now

            if pending_first_clap is None:
                pending_first_clap = now
                return

            if now - pending_first_clap <= DOUBLE_CLAP_WINDOW_S:
                pending_first_clap = None
                try:
                    on_double_clap()
                except Exception as e:
                    print(f"[ClapListen] on_double_clap callback error: {e}")
            else:
                pending_first_clap = now  # too slow -- treat this as a new first clap

        try:
            with sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=1,
                dtype="float32",
                blocksize=_block_size,
                device=device,
                callback=callback,
            ):
                print("[ClapListen] Listening for double-claps...")
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
