# Glove price model

This directory is reserved for the isolated glove-only model. The trainer is
`train_glove_price_model.py`; staging candidates and promoted generations must
declare `model_name=gloves` and `item_kind=glove`. Weapon-skin and knife models
are never used as a fallback.
