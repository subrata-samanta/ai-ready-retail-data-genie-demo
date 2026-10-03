# FreshCart, as a project made from the Genie template

This folder applies [`genie_template/`](../../genie_template/) to this repository's own Genie space, the
*FreshCart Sales & Stock Assistant*, on the data that the data bundle (`databricks/`) builds. It is also how
the template is tested on a real project (`tests/test_genie_template_freshcart.py`).

| File | What it is |
|---|---|
| `genie.config.yml` | the template's config, filled in for FreshCart (the only file that differs from the template) |
| `make_project.py` | builds the FreshCart Genie project: the template + this config + FreshCart's space in the template's form |

```bash
python examples/freshcart_genie_project/make_project.py ../freshcart-genie
```

The new folder is a complete project: copy it to the root of a new GitHub repository, add the repository to
Databricks as a Git folder, and run `notebooks/Genie_Project_Template` (see its README).

## The FreshCart values

| Setting | Value | Why |
|---|---|---|
| project | `freshcart-genie` | service principals `freshcart-genie-deployer-dev`, `-qa` and `-prod` |
| catalogs | `freshcart_dev`, `freshcart_qa`, `freshcart` | the data bundle's catalogs |
| schema | `semantic` (the space also uses `gold`) | the metric views; `gold` keeps its name in every environment |
| groups | `freshcart-business-users`, `freshcart-genie-developers`, `freshcart-genie-deployers` | the groups the end-to-end notebook creates |
| `deployer_extra_groups` | `fc_all_regions` | FreshCart's row filters show rows only to region groups; the quality gate must see every region |
| gate | 60% accuracy, at least 4 graded | as in `genie_bundle/` |
| smoke question | *What were net sales and margin by region last week?* | an example question with trusted SQL |
| `max_data_age_hours` | 26 | the refresh job loads daily at 05:00 UTC |
| `monitor_cron` | 06:30 UTC | after the refresh |

`python scripts/readiness.py` in the project shows two open items: the prod alert recipients (`alert_emails`,
`alert_subscribers`). Fill them in before going live.

## What the tests prove

- **The project is correct.** Its own tests, validation and readiness review pass. The space it deploys is, for
  dev, qa and prod, exactly what `genie_bundle/` deploys.
- **The queries are correct for FreshCart data.** Every example SQL (parameterised examples with their default
  values) and every one of the 17 benchmark answers runs on FreshCart's data for every environment, and every
  benchmark returns rows.
- **The bundle works.** With the real Databricks CLI against the stand-in workspace, it validates every target and
  deploys qa with the dashboard and alerts on `freshcart_qa.genie_monitoring`. Then:
  - the quality gate passes on FreshCart's benchmarks;
  - the smoke test answers with SQL;
  - the usage job and the approver's report run;
  - exporting the deployed space gives back the file in git, unchanged, so syncs make no needless changes.
- **The notebook works for FreshCart.** It runs top to bottom with `apply_changes = yes`:
  - setup: the deployers are put in `fc_all_regions` and granted `gold` and `semantic`, and no TPC-H example
    data is created even though `example_data` is `yes`;
  - all 23 queries run on FreshCart data;
  - a FreshCart benchmark question is released to prod on the `freshcart` catalog;
  - a dev UI edit comes back as exactly one change;
  - drift is blocked, and the rollback restores the previous release.
- **Importing gives the same result.** Importing the FreshCart space from a dev prototype (widget
  `import_space_id`) produces byte for byte the same file as `make_project.py`.

## Moving from `genie_bundle/` to the template

Both deploy a space with the same title. Use one or the other in a workspace:

1. Build the project, then set it up and release it with the template notebook.
2. Run `databricks bundle destroy -t <env>` for `genie_bundle/`, as its deployer.

Conversations in the old spaces are not moved. To keep both for a while, give the template's `space_title` a
different name.
