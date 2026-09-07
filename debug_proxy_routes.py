import json
import argparse
import urllib.parse
from pathlib import Path

import requests
import app_config


CONFIG_PATH = str(app_config.path("CSFLOAT_ROUTES_PATH", "api_keys_proxies.txt"))
IP_CHECK_URL = "https://api.ipify.org?format=json"


def mask_proxy(proxy_url):
    if not proxy_url:
        return "direct"

    parsed = urllib.parse.urlsplit(proxy_url)
    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    user = "***@" if parsed.username else ""
    return f"{parsed.scheme}://{user}{host}{port}"


def load_routes():
    path = Path(CONFIG_PATH)
    if not path.exists():
        raise SystemExit(
            f"{CONFIG_PATH} not found.\n"
            f"Create it from api_keys_proxies.example.txt and put your real SOCKS5 proxies there."
        )

    routes = []

    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        parts = [part.strip() for part in line.split("|", 1)]
        key_path = parts[0]
        proxy_url = parts[1] if len(parts) == 2 else ""

        routes.append({
            "line": line_number,
            "key_path": key_path,
            "proxy_url": proxy_url,
            "proxy_label": mask_proxy(proxy_url),
        })

    return routes


def check_ip(route, timeout):
    session = requests.Session()
    session.trust_env = False

    if route["proxy_url"]:
        session.proxies = {
            "http": route["proxy_url"],
            "https": route["proxy_url"],
        }

    response = session.get(IP_CHECK_URL, timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    return payload.get("ip") or json.dumps(payload, ensure_ascii=False)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--key", help="Check only one key file name, for example key3")
    parser.add_argument("--timeout", type=int, default=15)
    args = parser.parse_args()

    routes = load_routes()

    if args.key:
        routes = [route for route in routes if route["key_path"] == args.key]

    if not routes:
        raise SystemExit(f"{CONFIG_PATH} has no active routes for this filter")

    for route in routes:
        print(f"{route['key_path']} -> {route['proxy_label']}", flush=True)

        try:
            ip = check_ip(route, args.timeout)
            print(f"  OUTBOUND IP: {ip}", flush=True)
        except Exception as exc:
            print(f"  ERROR: {exc}", flush=True)


if __name__ == "__main__":
    main()
