"""Unit tests for core/utils.py — shared project utilities.

Tests the canonical get_base_dir(), get_api_config(), and load_api_key()
functions that replaced 15+ duplicated implementations across the project.
"""

import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

# Ensure project root is on sys.path so core.utils is importable
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from core.utils import (
    BASE_DIR,
    CONFIG_PATH,
    get_api_config,
    get_base_dir,
    load_api_key,
)


class TestGetBaseDir(unittest.TestCase):
    """get_base_dir() must resolve to an existing project root."""

    def test_returns_path(self):
        result = get_base_dir()
        self.assertIsInstance(result, Path)

    def test_path_exists(self):
        self.assertTrue(get_base_dir().exists())

    def test_is_absolute(self):
        self.assertTrue(get_base_dir().is_absolute())

    def test_contains_config_dir(self):
        """The project root should contain a config/ subdirectory."""
        self.assertTrue((get_base_dir() / "config").is_dir())

    def test_base_dir_matches_get_base_dir(self):
        """Module-level BASE_DIR constant should equal get_base_dir()."""
        self.assertEqual(BASE_DIR, get_base_dir())

    @patch("core.utils.sys.frozen", True, create=True)
    @patch("core.utils.sys.executable", "/opt/jeeves/jeeves.exe")
    def test_frozen_uses_executable_parent(self):
        """When frozen (PyInstaller), base_dir = parent of sys.executable."""
        result = get_base_dir()
        self.assertEqual(result, Path("/opt/jeeves"))


class TestConfigPath(unittest.TestCase):
    """CONFIG_PATH must resolve correctly."""

    def test_config_path_ends_correctly(self):
        self.assertEqual(CONFIG_PATH.name, "api_keys.json")
        self.assertEqual(CONFIG_PATH.parent.name, "config")

    def test_config_path_uses_base_dir(self):
        self.assertEqual(CONFIG_PATH, BASE_DIR / "config" / "api_keys.json")


class TestGetApiConfig(unittest.TestCase):
    """get_api_config() must always return a dict."""

    def test_returns_dict(self):
        config = get_api_config()
        self.assertIsInstance(config, dict)

    def test_contains_expected_keys(self):
        """If config file exists, it should at least have groq_api_key."""
        config = get_api_config()
        if config:
            self.assertIn("groq_api_key", config)

    @patch("core.utils.CONFIG_PATH", Path("/nonexistent/path/api_keys.json"))
    def test_missing_file_returns_empty_dict(self):
        self.assertEqual(get_api_config(), {})

    @patch("core.utils.CONFIG_PATH")
    def test_corrupted_file_returns_empty_dict(self, mock_path):
        mock_path.exists.return_value = True
        mock_path.read_text.return_value = "not valid json {{{"
        self.assertEqual(get_api_config(), {})


class TestLoadApiKey(unittest.TestCase):
    """load_api_key() must handle present, absent, and fallback keys."""

    def test_returns_string(self):
        key = load_api_key("groq_api_key")
        self.assertIsInstance(key, str)

    def test_nonexistent_key_returns_empty(self):
        self.assertEqual(load_api_key("this_key_does_not_exist_xyz"), "")

    def test_nonexistent_key_with_fallback(self):
        """When primary key is missing, fallback_keys should be tried."""
        with patch("core.utils.get_api_config", return_value={
            "groq_api_key": "real_groq_key",
        }):
            key = load_api_key(
                "nonexistent_primary",
                fallback_keys=["another_nonexistent", "groq_api_key"],
            )
            self.assertEqual(key, "real_groq_key")

    def test_fallback_no_keys_returns_empty(self):
        """No fallback_keys and no matching key."""
        self.assertEqual(load_api_key("nope"), "")

    def test_empty_fallback_list_skips_fallback(self):
        """Explicit empty fallback list should not be treated as None."""
        # This tests the `if not key and fallback_keys:` branch —
        # an empty list is falsy, so fallback is skipped.
        with patch("core.utils.get_api_config", return_value={}):
            self.assertEqual(load_api_key("anything", fallback_keys=[]), "")

    def test_fallback_takes_precedence_over_empty_primary(self):
        """When primary key exists but is empty, fallback should be used."""
        config_data = {"groq_api_key": "", "backup_key": "backup_value"}
        with patch("core.utils.get_api_config", return_value=config_data):
            key = load_api_key("groq_api_key", fallback_keys=["backup_key"])
            self.assertEqual(key, "backup_value")


class TestIntegration(unittest.TestCase):
    """Smoke tests that validate real-world usage patterns."""

    def test_get_config_then_load_key(self):
        """Realistic usage: load config once, then read multiple keys."""
        config = get_api_config()
        if not config:
            self.skipTest("config/api_keys.json not found or empty")
        groq_key = load_api_key("groq_api_key")
        self.assertIsInstance(groq_key, str)
        # If groq_key is set, it should be non-empty
        if "groq_api_key" in config:
            self.assertNotEqual(groq_key, "" if config["groq_api_key"] else "")


if __name__ == "__main__":
    unittest.main()
