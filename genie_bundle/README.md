# freshcart-genie bundle

The FreshCart Genie space, its quality-gate job and all environments (sandbox, dev, qa, prod), as one
Declarative Automation Bundle. The full guide is
[`notebooks/Genie_CICD_with_Declarative_Automation_Bundles.ipynb`](../notebooks/Genie_CICD_with_Declarative_Automation_Bundles.ipynb).

```bash
databricks bundle validate -t qa
databricks bundle plan     -t qa
databricks bundle deploy   -t qa
databricks bundle run genie_quality_gate -t qa                         # benchmark gate
databricks bundle run genie_quality_gate -t prod --params mode=smoke   # smoke test
python scripts/sync_from_workspace.py -t dev                           # UI edits in dev -> git
python scripts/validate_space.py --sql                                 # checks used in CI
```
