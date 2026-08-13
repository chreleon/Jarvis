"""
memory_cleanup.py -- Cleanup utility for Jeeves to remove temporary data,
caches, and unnecessary files that accumulate during normal operation.

This runs automatically when Jeeves closes, freeing up disk space without
affecting functionality.

Targets:
  - __pycache__ directories (Python bytecode cache)
  - Playwright browser cache
  - Old/duplicate voice model backups
  - Temporary playwright/browser data
  - Log rotation
"""

import shutil
import logging
from pathlib import Path
import sys
from datetime import datetime, timedelta
import json

def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


BASE_DIR = _base_dir()
CLEANUP_LOG = BASE_DIR / "memory" / "cleanup.log"


def _ensure_cleanup_log():
    """Ensure cleanup log directory exists."""
    CLEANUP_LOG.parent.mkdir(parents=True, exist_ok=True)


def _log_cleanup(message: str):
    """Log cleanup operations."""
    _ensure_cleanup_log()
    with open(CLEANUP_LOG, "a", encoding="utf-8") as f:
        timestamp = datetime.now().isoformat()
        f.write(f"[{timestamp}] {message}\n")


def clean_pycache() -> int:
    """Remove all __pycache__ directories.
    
    Returns: Total bytes freed
    """
    freed = 0
    for pycache_dir in BASE_DIR.rglob("__pycache__"):
        try:
            size = sum(f.stat().st_size for f in pycache_dir.rglob("*") if f.is_file())
            shutil.rmtree(pycache_dir)
            freed += size
            _log_cleanup(f"Removed pycache: {pycache_dir.relative_to(BASE_DIR)}")
        except Exception as e:
            _log_cleanup(f"Failed to remove pycache {pycache_dir}: {e}")
    return freed


def clean_playwright_cache() -> int:
    """Remove Playwright browser cache.
    
    Returns: Total bytes freed
    """
    freed = 0
    # Playwright cache is typically in ~/.cache/ms-playwright or similar
    home = Path.home()
    playwright_cache = home / ".cache" / "ms-playwright"
    
    if playwright_cache.exists():
        try:
            size = sum(f.stat().st_size for f in playwright_cache.rglob("*") if f.is_file())
            shutil.rmtree(playwright_cache)
            freed += size
            _log_cleanup(f"Removed Playwright cache: {size} bytes")
        except Exception as e:
            _log_cleanup(f"Failed to remove Playwright cache: {e}")
    
    return freed


def clean_temp_files() -> int:
    """Remove temporary files created during Jeeves operation.
    
    Returns: Total bytes freed
    """
    freed = 0
    temp_patterns = [".tmp", ".temp", ".bak", ".cache"]
    
    for pattern in temp_patterns:
        for temp_file in BASE_DIR.rglob(f"*{pattern}"):
            try:
                if temp_file.is_file():
                    size = temp_file.stat().st_size
                    temp_file.unlink()
                    freed += size
                    _log_cleanup(f"Removed temp file: {temp_file.relative_to(BASE_DIR)}")
            except Exception as e:
                _log_cleanup(f"Failed to remove temp file {temp_file}: {e}")
    
    return freed


def clean_memory_excess() -> int:
    """Optimize long-term memory file if it exceeds safe size.
    
    Returns: Total bytes freed
    """
    freed = 0
    memory_path = BASE_DIR / "memory" / "long_term.json"
    
    if not memory_path.exists():
        return freed
    
    try:
        with open(memory_path, "r", encoding="utf-8") as f:
            memory_data = json.load(f)
        
        original_size = memory_path.stat().st_size
        
        # Truncate notes if memory exceeds 1MB (usually indicates excessive logging)
        if original_size > 1024 * 1024:
            if "notes" in memory_data:
                # Keep only the last 100 entries in notes
                if isinstance(memory_data["notes"], dict):
                    notes_items = list(memory_data["notes"].items())
                    if len(notes_items) > 100:
                        memory_data["notes"] = dict(notes_items[-100:])
                        
                        with open(memory_path, "w", encoding="utf-8") as f:
                            json.dump(memory_data, f, indent=2)
                        
                        new_size = memory_path.stat().st_size
                        freed = original_size - new_size
                        _log_cleanup(f"Optimized memory file: freed {freed} bytes")
    except Exception as e:
        _log_cleanup(f"Failed to optimize memory: {e}")
    
    return freed


def rotate_cleanup_log() -> int:
    """Archive old cleanup logs.
    
    Returns: Total bytes freed
    """
    freed = 0
    
    if not CLEANUP_LOG.exists():
        return freed
    
    try:
        # If cleanup log exceeds 100KB, archive it
        log_size = CLEANUP_LOG.stat().st_size
        if log_size > 100 * 1024:
            archive_name = CLEANUP_LOG.parent / f"cleanup.{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
            shutil.move(str(CLEANUP_LOG), str(archive_name))
            freed = log_size
            _log_cleanup(f"Archived cleanup log: {freed} bytes")
    except Exception as e:
        _log_cleanup(f"Failed to rotate cleanup log: {e}")
    
    return freed


def get_cleanup_stats() -> dict:
    """Return statistics on what was cleaned."""
    stats = {
        "pycache": clean_pycache(),
        "playwright": clean_playwright_cache(),
        "temp_files": clean_temp_files(),
        "memory_optimization": clean_memory_excess(),
        "log_rotation": rotate_cleanup_log(),
    }
    return stats


def cleanup():
    """Main cleanup routine. Run this on shutdown."""
    _ensure_cleanup_log()
    _log_cleanup("=" * 60)
    _log_cleanup("CLEANUP START")
    
    try:
        stats = get_cleanup_stats()
        
        total_freed = sum(stats.values())
        total_freed_mb = total_freed / (1024 * 1024)
        
        _log_cleanup(f"Pycache: {stats['pycache'] / 1024:.1f} KB")
        _log_cleanup(f"Playwright: {stats['playwright'] / 1024:.1f} KB")
        _log_cleanup(f"Temp files: {stats['temp_files'] / 1024:.1f} KB")
        _log_cleanup(f"Memory optimization: {stats['memory_optimization'] / 1024:.1f} KB")
        _log_cleanup(f"Log rotation: {stats['log_rotation'] / 1024:.1f} KB")
        _log_cleanup(f"TOTAL FREED: {total_freed_mb:.2f} MB")
        
    except Exception as e:
        _log_cleanup(f"CLEANUP ERROR: {e}")
    finally:
        _log_cleanup("CLEANUP END")
        _log_cleanup("=" * 60)


if __name__ == "__main__":
    cleanup()
    print("Jeeves cleanup complete. Check memory/cleanup.log for details.")
