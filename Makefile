.PHONY: demo data pipeline test docs databricks

demo:        ## pipeline + checks + benchmarks + exports + docs
	python run_demo.py

data:        ## regenerate the synthetic raw data (same seed, same files)
	python -m freshcart.generate

pipeline:    ## bronze -> silver -> gold from scratch
	python -m freshcart.pipeline --full-refresh

test:
	python tests/run_tests.py

docs:
	python -m freshcart.docs

databricks:  ## regenerate Databricks DDL from contracts and metric-view YAML
	python -m freshcart.export_databricks
