"""Train a staging candidate using glove rows only."""

from item_classification import ITEM_KIND_GLOVE
from train_price_model import parse_args, run_training


def main(argv=None) -> None:
    args = parse_args(argv, default_item_kind=ITEM_KIND_GLOVE)
    if args.item_kind != ITEM_KIND_GLOVE:
        raise ValueError("train_glove_price_model accepts glove data only")
    run_training(args)


if __name__ == "__main__":
    main()
