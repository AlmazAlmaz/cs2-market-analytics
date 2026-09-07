# Dedicated knife model

Knife prices are intentionally isolated from weapon-skin training and runtime
artifacts. The parser first fills `cs2_item_catalog` and collects knife sales;
training remains unavailable until there is enough chronological data for an
untouched holdout. No weapon-skin artifact is used as a knife fallback.

The first training run requires at least 16,000 knife sales and 16,000 rows
whose reference snapshot is provably available no later than the sale. The
trainer reserves the newest 5,000 rows as an untouched test set; activation is
still refused if absolute error and interval-coverage gates are not met.

Runtime artifacts:

- `artifacts/knife_price_model.joblib`
- `artifacts/knife_price_model_metrics.json`

Until a candidate passes its own quality and integrity gates, the Knives page
uses observed market history only and labels the dedicated model as collecting
data.
