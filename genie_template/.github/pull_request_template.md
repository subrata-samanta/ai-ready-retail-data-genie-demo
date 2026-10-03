## What changes in the Genie space

<!-- The check "genie-ci / checks" writes the exact change list into its summary. Say here why. -->

## Why

<!-- The user need, a failed or thumbs-down question from the usage dashboard, a new data source... -->

## Checklist

- [ ] Tried in my sandbox (`databricks bundle deploy -t sandbox`), the new or changed questions answer correctly
- [ ] New kinds of question have a **benchmark** with a trusted SQL answer (they are the quality gate)
- [ ] Example SQL and benchmark answers use `${var.catalog}.${var.schema}.<table>`, never a real catalog name
- [ ] `python scripts/validate_space.py` and `python tests/run_tests.py` pass
- [ ] Changes to `genie.config.yml` (thresholds, groups, alerts) are explained above
