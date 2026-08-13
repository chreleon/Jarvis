"""register_composio_tool.py

Helper to register Jeeves as an external tool in a Composio account.

Usage:
    python agent/register_composio_tool.py --url https://your-jeeves-host --path /invoke
    python agent/register_composio_tool.py --url https://your-jeeves-host --path /call

The script will attempt to use the installed `composio` SDK to create a
tool entry. If the SDK in the environment doesn't support programmatic
registration, the script writes a JSON descriptor and prints manual
registration instructions.
"""

import argparse
import json
import os
import sys
from urllib.parse import urlsplit, urlunsplit
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parent.parent / 'config' / 'api_keys.json'


def load_config():
    try:
        return json.loads(CONFIG_PATH.read_text(encoding='utf-8'))
    except Exception:
        return {}


def _join_endpoint(base_url: str, endpoint_path: str) -> str:
    parts = urlsplit(base_url)
    path = f"/{endpoint_path.lstrip('/')}"
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))


def build_tool_descriptor(name: str, url: str, api_secret: str):
    # Minimal descriptor describing an HTTP tool that posts to the selected endpoint.
    return {
        "name": name,
        "description": "Jeeves assistant: exposes /invoke for sync/async agent calls.",
        "endpoint": url,
        "auth": {
            "type": "header",
            "name": "Authorization",
            "value_template": "Bearer {{secret}}"
        },
        "security": {
            "shared_secret_hint": "Provide the Jeeves API secret as the tool credential"
        },
        "input_schema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string"},
                "system_prompt": {"type": ["string", "null"]},
                "mode": {"type": "string", "enum": ["sync", "async"]},
                "callback_url": {"type": ["string", "null"]}
            },
            "required": ["prompt"]
        },
        "output_schema": {"type": "object", "properties": {"result": {"type": "string"}}}
    }


def try_register_via_sdk(api_key: str, user_id: str, descriptor: dict):
    try:
        from composio import Composio
        client = Composio(api_key=api_key)
        tools = getattr(client, 'tools', None)
        if tools is None:
            return False, 'no_tools'

        if hasattr(tools, 'create'):
            tools.create(user_id=user_id, body=descriptor)
            return True, 'created'

        if hasattr(tools, 'register'):
            tools.register(user_id=user_id, descriptor=descriptor)
            return True, 'registered'

        if hasattr(tools, 'put'):
            tools.put(user_id=user_id, body=descriptor)
            return True, 'put'

        return False, 'no_method'
    except Exception as e:
        return False, str(e)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', required=True, help='Public base URL for Jeeves (for example https://your-host)')
    parser.add_argument('--path', default='/invoke', help='Endpoint path to register, such as /invoke or /call')
    parser.add_argument('--name', default='jeeves', help='Tool name to register in Composio')
    args = parser.parse_args()

    cfg = load_config()
    api_key = os.environ.get('COMPOSIO_API_KEY') or cfg.get('composio_api_key')
    user_id = os.environ.get('COMPOSIO_USER_ID') or cfg.get('composio_user_id') or 'default'
    api_secret = os.environ.get('JEEVES_API_SECRET') or cfg.get('jeeves_api_secret')

    if not api_key:
        print('Composio API key not found in environment or config/api_keys.json. Set COMPOSIO_API_KEY and try again.')
        sys.exit(1)

    if not api_secret:
        print('Jeeves API secret not found in config; please set jeeves_api_secret in config/api_keys.json')
        sys.exit(1)

    endpoint_url = _join_endpoint(args.url, args.path)
    descriptor = build_tool_descriptor(args.name, endpoint_url, api_secret)

    print('[register] Attempting to register Jeeves tool via Composio SDK...')
    ok, info = try_register_via_sdk(api_key, user_id, descriptor)
    if ok:
        print(f'[register] Success: {info}')
        return

    print('[register] SDK registration not available or failed:', info)
    out_path = Path.cwd() / f'{args.name}_composio_tool.json'
    out_path.write_text(json.dumps(descriptor, indent=2), encoding='utf-8')
    print(f'[register] Wrote descriptor to {out_path}.')
    print('\nManual registration steps:')
    print('1. Open Composio dashboard and create a new custom tool.')
    print('2. Use the following values:')
    print('   - Name:', args.name)
    print('   - Endpoint URL:', endpoint_url)
    print('   - Auth header: Authorization: Bearer <jeeves_api_secret>')
    print('3. Paste the content of the descriptor file that was just written if the UI accepts a JSON descriptor.')


if __name__ == '__main__':
    main()
