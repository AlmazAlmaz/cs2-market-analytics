"""Train a staging candidate using knife rows only."""

from item_classification import ITEM_KIND_KNIFE
from train_price_model import parse_args, run_training


def main(argv=None) -> None:
    args = parse_args(argv, default_item_kind=ITEM_KIND_KNIFE)
    if args.item_kind != ITEM_KIND_KNIFE:
        raise ValueError("train_knife_price_model accepts knife data only")
    run_training(args)


if __name__ == "__main__":
    main()
