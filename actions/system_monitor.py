"""
system_monitor.py -- Real-time system status for the `system_status` tool.

CPU / RAM / GPU / temperature / uptime / process-count snapshot.
Zero subprocess calls — uses psutil (+ optional pynvml for NVIDIA GPUs).
"""

import platform
import time

import psutil

_OS = platform.system()  # "Windows" | "Darwin" | "Linux"


def _get_gpu_usage() -> float:
    """GPU utilisation % via pynvml (NVIDIA). Returns -1.0 if unavailable."""
    try:
        import pynvml  # type: ignore
        pynvml.nvmlInit()
        h = pynvml.nvmlDeviceGetHandleByIndex(0)
        return float(pynvml.nvmlDeviceGetUtilizationRates(h).gpu)
    except Exception:
        return -1.0


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
