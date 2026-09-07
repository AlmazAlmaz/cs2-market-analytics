import argparse
import json
import sqlite3

import price_model_runtime
import app_config


DB_PATH = str(app_config.path("CS2_DB_PATH", "csfloat_history.db"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--name")
    parser.add_argument("--url-text", default="")
    parser.add_argument("--buy-price-usd")
    parser.add_argument("--fee-percent", default="4")
    parser.add_argument("--base-skin-name")
    parser.add_argument("--wear-name")
    parser.add_argument("--float", dest="float_value")
    parser.add_argument("--paint-index")
    parser.add_argument("--paint-seed")
    parser.add_argument("--reference-base-price")
    parser.add_argument("--stickers", default="[]")
    args = parser.parse_args()

    payload = {
        "name": args.name or "",
        "url_text": args.url_text,
        "buy_price_usd": args.buy_price_usd,
        "fee_percent": args.fee_percent,
        "base_skin_name": args.base_skin_name,
        "wear_name": args.wear_name,
        "float_value": args.float_value,
        "paint_index": args.paint_index,
        "paint_seed": args.paint_seed,
        "reference_base_price": args.reference_base_price,
        "stickers": json.loads(args.stickers),
    }

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        result = price_model_runtime.predict_deal(payload, conn)

    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
