"""
composio_connect.py -- One-click "Connect my account" buttons for the setup
screen. Uses the shared ComposioToolSet from composio_shim.py.

For each app, this asks Composio for an authorization URL and opens it in
the person's default browser -- the same kind of OAuth screen you'd see
connecting any third-party account.
"""

import threading
import webbrowser

from composio_shim import ComposioToolSet

FALLBACK_DASHBOARD_URL = "https://app.composio.dev"

_APP_MAP = {
    "github":         "GITHUB",
    "gmail":          "GMAIL",
    "googlecalendar": "GOOGLECALENDAR",
}


def connect_app(app_key: str, status_callback=None) -> bool:
    """
    Kicks off a Composio OAuth connection for app_key ('github', 'gmail',
    or 'googlecalendar'), opening the authorization page in the browser.
    Returns True if a browser window was opened, False otherwise.
    """
    def _report(msg: str):
        if status_callback:
            status_callback(msg)
        print(f"[ComposioConnect] {msg}")

    app_name = _APP_MAP.get((app_key or "").strip().lower())
    if not app_name:
        _report(f"Unknown app: {app_key!r}. Supported: {', '.join(sorted(_APP_MAP))}.")
        return False

    try:
        toolset = ComposioToolSet()

        try:
            request = toolset.initiate_connection(app=app_name)
        except AttributeError:
            # No usable SDK backend behind the shim -- open dashboard.
            _report("Composio SDK not available. Opening dashboard instead.")
            webbrowser.open(FALLBACK_DASHBOARD_URL)
            return False
        except Exception as e:
            _report(f"Couldn't start the {app_key} connection automatically ({e}). "
                     "Opening the Composio dashboard instead -- you can connect it there.")
            webbrowser.open(FALLBACK_DASHBOARD_URL)
            return False

        if isinstance(request, dict):
            redirect_url = request.get("redirectUrl") or request.get("redirect_url")
        else:
            redirect_url = getattr(request, "redirectUrl", None) or \
                getattr(request, "redirect_url", None)

        if redirect_url:
            _report(f"Opening browser to connect {app_key}...")
            webbrowser.open(str(redirect_url))
            return True

        _report(f"{app_key} may already be connected, or no authorization step was needed.")
        return True

    except Exception as e:
        _report(f"Unexpected error while connecting {app_key}: {e}")
        return False


def connect_app_async(app_key: str, status_callback=None):
    """Runs connect_app() in a background thread so the UI doesn't freeze."""
    threading.Thread(target=connect_app, args=(app_key, status_callback), daemon=True).start()


if __name__ == "__main__":
    connect_app("github", print)
