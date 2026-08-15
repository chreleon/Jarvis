"""
system_monitor.py -- Real-time system status for the `system_status` tool
and threshold-based background alerts for the `SystemMonitor` class.

CPU / RAM / GPU / temperature / uptime / process-count snapshot.
Zero subprocess calls — uses psutil, pynvml, or the NVML DLL directly
(ctypes) for NVIDIA GPUs.
"""

import ctypes
import platform
import time

import psutil

_OS = platform.system()  # "Windows" | "Darwin" | "Linux"

# ── NVML DLL cache (Windows: nvml.dll, Linux: libnvidia-ml.so.1) ─────────────
_nvml_lib: object = None
_nvml_ok:  object = None   # None=untested  True=works  False=unavailable


def _nvml_gpu() -> float:
    """GPU utilisation via NVML ctypes — zero subprocess on all platforms."""
    global _nvml_lib, _nvml_ok
    if _nvml_ok is False:
        return -1.0
    try:
        class _Util(ctypes.Structure):
            _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]

        if _nvml_lib is None:
            if _OS == "Windows":
                candidates = ("nvml", r"C:\Windows\System32\nvml.dll")
                _load = ctypes.WinDLL
            else:
                candidates = (
                    "libnvidia-ml.so.1",
                    "libnvidia-ml.so",
                    "libnvidia-ml.dylib",
                )
                _load = ctypes.CDLL
            for name in candidates:
                try:
                    lib = _load(name)
                    if lib.nvmlInit_v2() != 0:
                        continue  # driver present but init failed — try next candidate
                    _nvml_lib = lib
                    break
                except Exception:
                    continue

        if _nvml_lib is None:
            _nvml_ok = False
            return -1.0

        dev = ctypes.c_void_p()
        if _nvml_lib.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(dev)) != 0:
            _nvml_ok = False
            return -1.0

        u = _Util()
        if _nvml_lib.nvmlDeviceGetUtilizationRates(dev, ctypes.byref(u)) != 0:
            _nvml_ok = False
            return -1.0

        _nvml_ok = True
        return float(u.gpu)
    except Exception:
        _nvml_ok = False
        return -1.0


def _get_gpu_usage() -> float:
    """GPU utilisation %. pynvml first, then ctypes NVML DLL. -1.0 if unavailable."""
    try:
        import pynvml  # type: ignore
        pynvml.nvmlInit()
        h = pynvml.nvmlDeviceGetHandleByIndex(0)
        return float(pynvml.nvmlDeviceGetUtilizationRates(h).gpu)
    except Exception:
        pass

    return _nvml_gpu()


def _get_cpu_temp() -> float:
    """CPU temperature via psutil (Linux) or WMI (Windows). -1.0 if unavailable."""
    try:
        temps = psutil.sensors_temperatures()
        for name in ["coretemp", "k10temp", "cpu_thermal", "acpitz",
                     "cpu-thermal", "zenpower", "it8688"]:
            if name in temps and temps[name]:
                return temps[name][0].current
        for entries in temps.values():
            if entries:
                return entries[0].current
    except Exception:
        pass

    if _OS == "Windows":
        try:
            import wmi  # type: ignore
            w = wmi.WMI(namespace="root/wmi")
            tz = w.MSAcpi_ThermalZoneTemperature()
            if tz:
                return (tz[0].CurrentTemperature / 10.0) - 273.15
        except Exception:
            pass

    return -1.0


def get_system_status() -> dict:
    """Snapshot of current system metrics for the system_status tool."""
    cpu  = psutil.cpu_percent(interval=0.2)
    ram  = psutil.virtual_memory()
    temp = _get_cpu_temp()
    gpu  = _get_gpu_usage()

    uptime_secs = time.time() - psutil.boot_time()
    uptime_h    = int(uptime_secs // 3600)
    uptime_m    = int((uptime_secs % 3600) // 60)

    return {
        "cpu_percent":   round(cpu, 1),
        "ram_percent":   round(ram.percent, 1),
        "ram_used_gb":   round(ram.used   / 1024 ** 3, 1),
        "ram_total_gb":  round(ram.total  / 1024 ** 3, 1),
        "cpu_temp_c":    round(temp, 1) if temp > 0 else None,
        "gpu_percent":   round(gpu,  1) if gpu  >= 0 else None,
        "uptime":        f"{uptime_h}h {uptime_m}m",
        "process_count": len(psutil.pids()),
    }


def _format_status(s: dict) -> str:
    """Render the status snapshot as a readable string for speech/log."""
    lines = [
        f"CPU: {s['cpu_percent']}%",
        f"RAM: {s['ram_percent']}% used ({s['ram_used_gb']} of {s['ram_total_gb']} GB)",
    ]
    if s.get("cpu_temp_c") is not None:
        lines.append(f"CPU temp: {s['cpu_temp_c']}°C")
    if s.get("gpu_percent") is not None:
        lines.append(f"GPU: {s['gpu_percent']}%")
    lines.append(f"Uptime: {s['uptime']}")
    lines.append(f"Processes: {s['process_count']}")
    return "\n".join(lines)


def system_status(
    parameters: dict | None = None,
    response=None,
    player=None,
    session_memory=None,
    speak=None,
) -> str:
    """Action entry point for the `system_status` tool.

    Snapshots CPU / RAM / GPU / temperature / uptime and returns a readable
    summary. Pure psutil — no subprocesses, no network, no Gemini.
    """
    params = parameters or {}
    try:
        snapshot = get_system_status()
    except Exception as e:
        return f"Could not read system status, sir: {e}"

    text = _format_status(snapshot)

    if speak:
        speak(text)
    if player:
        player.write_log(f"[sys] {text.replace(chr(10), ' | ')}")
    return text


# ── Background threshold alerts ───────────────────────────────────────────────

DEFAULT_THRESHOLDS = {
    "cpu":  90.0,
    "ram":  90.0,
    "temp": 85.0,
    "gpu":  95.0,
}

_ALERT_COOLDOWN = 300     # seconds between alerts per metric
_CPU_STREAK     = 3       # consecutive high-CPU polls before alerting


class SystemMonitor:
    """
    Stateful background monitor — checks hardware metrics against thresholds
    and returns a [SYSTEM_ALERT] string (or None) when one is crossed.

    Cooldown state persists for the lifetime of the process, so repeated
    crossings never spam the user. CPU needs 3 consecutive high polls to
    avoid alerting on a single transient spike.

    Call check() periodically (e.g. every 10s from a background task); it
    is cheap — psutil reads only, no subprocesses.
    """

    def __init__(self, thresholds: dict | None = None):
        self.thresholds   = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
        self._last_alert: dict[str, float] = {}
        self._cpu_streak  = 0
        # Last fired alert (for the remote dashboard / UI display). Set
        # whenever check() returns a non-empty alert; read from other
        # threads — simple string attr, atomic under the GIL.
        self.last_alert: str | None = None
        self.last_alert_at: float | None = None

    def _can_alert(self, key: str) -> bool:
        return (time.monotonic() - self._last_alert.get(key, 0)) > _ALERT_COOLDOWN

    def _record(self, key: str):
        self._last_alert[key] = time.monotonic()

    def check(self) -> str | None:
        try:
            cpu  = psutil.cpu_percent(interval=None)
            ram  = psutil.virtual_memory().percent
            temp = _get_cpu_temp()
            gpu  = _get_gpu_usage()
        except Exception:
            return None

        alerts: list[str] = []

        if cpu >= self.thresholds["cpu"]:
            self._cpu_streak += 1
            if self._cpu_streak >= _CPU_STREAK and self._can_alert("cpu"):
                alerts.append(
                    f"[SYSTEM_ALERT] CPU usage has been critically high ({cpu:.0f}%) "
                    "for several seconds. Warn the user in their language and suggest "
                    "closing heavy applications."
                )
                self._record("cpu")
                self._cpu_streak = 0
        else:
            self._cpu_streak = 0

        if ram >= self.thresholds["ram"] and self._can_alert("ram"):
            alerts.append(
                f"[SYSTEM_ALERT] RAM is at {ram:.0f}% — nearly exhausted. "
                "Warn the user in their language and suggest freeing memory."
            )
            self._record("ram")

        if temp > 0 and temp >= self.thresholds["temp"] and self._can_alert("temp"):
            alerts.append(
                f"[SYSTEM_ALERT] CPU temperature is {temp:.0f}°C — above the safe limit. "
                "Warn the user in their language and advise reducing system load "
                "or checking cooling."
            )
            self._record("temp")

        if gpu >= 0 and gpu >= self.thresholds["gpu"] and self._can_alert("gpu"):
            alerts.append(
                f"[SYSTEM_ALERT] GPU load is at {gpu:.0f}%. "
                "Briefly inform the user in their language."
            )
            self._record("gpu")

        alert_text = " ".join(alerts) if alerts else None
        if alert_text:
            self.last_alert    = alert_text
            self.last_alert_at = time.time()
        return alert_text
