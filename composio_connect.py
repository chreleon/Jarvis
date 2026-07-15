"""
composio_connect.py -- One-click "Connect my account" buttons for the setup
screen. Uses the same composio_openai toolset that composio_agent.py already
depends on, so no new package is required.

For each app, this asks Composio for an authorization URL and opens it in
the person's default browser -- the same kind of OAuth screen you'd see
connecting any third-party account.
"""

import threading
import webbrowser

try:
    from composio_openai import ComposioToolSet, App
    _COMPOSIO_AVAILABLE = True
except Exception:
    _COMPOSIO_AVAILABLE = False

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

    if not _COMPOSIO_AVAILABLE:
        _report("Composio isn't installed yet (pip install composio-core composio-openai). "
                 "Opening the Composio dashboard instead.")
        webbrowser.open(FALLBACK_DASHBOARD_URL)
        return False

    app_name = _APP_MAP.get(app_key)
    if not app_name:
        _report(f"Unknown app: {app_key}")
        return False

    try:
        toolset = ComposioToolSet()
        app_enum = getattr(App, app_name)
        request = toolset.initiate_connection(app=app_enum)
        redirect_url = getattr(request, "redirectUrl", None) or getattr(request, "redirect_url", None)

        if redirect_url:
            _report(f"Opening browser to connect {app_key}...")
            webbrowser.open(redirect_url)
            return True
        else:
            _report(f"{app_key} may already be connected, or no authorization step was needed.")
            return True

    except Exception as e:
        _report(f"Couldn't start the {app_key} connection automatically ({e}). "
                 f"Opening the Composio dashboard instead -- you can connect it there.")
        webbrowser.open(FALLBACK_DASHBOARD_URL)
        return False


def connect_app_async(app_key: str, status_callback=None):
    """Runs connect_app() in a background thread so the UI doesn't freeze."""
    threading.Thread(target=connect_app, args=(app_key, status_callback), daemon=True).start()


if __name__ == "__main__":
    connect_app("github", print)
