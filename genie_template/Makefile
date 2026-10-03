# Everyday commands of the Genie project (run from this folder). `make help` lists them.
PY  ?= python
DBX ?= databricks
T   ?= sandbox

help:                 ## list the commands
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | sed 's/:.*##/\t/'

settings:             ## the project's settings per environment (genie.config.yml)
	$(PY) scripts/genie_tools.py

check:                ## everything genie-ci checks without a workspace
	$(PY) tests/run_tests.py
	$(PY) scripts/validate_space.py
	$(PY) scripts/build_notebooks.py --check
	$(PY) scripts/readiness.py --strict

fix:                  ## rewrite the space definition in canonical form; rebuild the notebooks
	$(PY) scripts/validate_space.py --fix
	$(PY) scripts/build_notebooks.py

diff:                 ## what this branch changes in the space, versus main
	$(PY) scripts/validate_space.py --diff-against main

readiness:            ## the production readiness review
	$(PY) scripts/readiness.py

sql:                  ## run every example and benchmark SQL on an environment's data (T=dev)
	$(PY) scripts/check_sql.py -t $(T)

deploy:               ## deploy your sandbox (T=sandbox; dev, qa and prod are deployed by GitHub Actions)
	$(DBX) bundle deploy -t $(T)

gate:                 ## run the quality job (health checks + benchmarks) on an environment
	$(DBX) bundle run genie_quality -t $(T)

sync:                 ## bring edits made in the environment's Genie UI into space/genie_space.yml
	$(PY) scripts/sync_from_workspace.py -t $(T)

destroy:              ## remove your sandbox
	$(DBX) bundle destroy -t $(T)

.PHONY: help settings check fix diff readiness sql deploy gate sync destroy
