import json
import sys
import urllib.error
import urllib.parse
import urllib.request

import app_config


API_KEY = app_config.read_secret("CSFLOAT_DEBUG_API_KEY", "key2")
OUTPUT_DIR = app_config.PROJECT_DIR
BASE_URL = "https://csfloat.com/api/v1/history"


def build_sales_url(market_hash_name):
    encoded_name = urllib.parse.quote(market_hash_name, safe="")
    return f"{BASE_URL}/{encoded_name}/sales"


def request_url(url):
    request = urllib.request.Request(url, headers={
        "Authorization": API_KEY,
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json",
    })

    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = response.read().decode("utf-8", errors="replace")

            (OUTPUT_DIR / "debug_success_response.txt").write_text(body, encoding="utf-8")

            print("STATUS:", response.status)
            print("HEADERS:")
            print(response.headers)
            print("BODY SAVED TO: debug_success_response.txt")

            try:
                data = json.loads(body)
                print("JSON TYPE:", type(data).__name__)

                if isinstance(data, list):
                    print("ITEMS:", len(data))

                    if data:
                        print("FIRST KEYS:", list(data[0].keys()))
                        print("FIRST SOLD_AT:", data[0].get("sold_at"))
                        print("LAST SOLD_AT:", data[-1].get("sold_at"))

                elif isinstance(data, dict):
                    print("DICT KEYS:", list(data.keys()))

            except json.JSONDecodeError:
                print("Response is not valid JSON.")

    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")

        text = (
            f"STATUS: {exc.code}\n"
            f"RETRY-AFTER: {exc.headers.get('Retry-After')}\n\n"
            f"HEADERS:\n{exc.headers}\n\n"
            f"BODY:\n{body}\n"
        )

        (OUTPUT_DIR / "debug_error_response.txt").write_text(text, encoding="utf-8")

        print(text)
        print("ERROR SAVED TO: debug_error_response.txt")

    except Exception as exc:
        text = f"{type(exc).__name__}: {exc}"
        (OUTPUT_DIR / "debug_network_error.txt").write_text(text, encoding="utf-8")

        print(text)
        print("NETWORK ERROR SAVED TO: debug_network_error.txt")


def main():
    if len(sys.argv) < 2:
        print('Usage:')
        print('  py debug_csfloat_response.py "AWP | Dragon Lore (Factory New)"')
        print('  py debug_csfloat_response.py "https://csfloat.com/api/v1/history/..."')
        raise SystemExit(1)

    arg = sys.argv[1]

    if arg.startswith("http://") or arg.startswith("https://"):
        url = arg
    else:
        url = build_sales_url(arg)

    print("REQUEST URL:")
    print(url)
    print()

    request_url(url)


if __name__ == "__main__":
    main()
