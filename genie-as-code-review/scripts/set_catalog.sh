#!/usr/bin/env bash
# Point the space JSON at a target's FreshCart catalog before deploying to that target.
#
# The JSON is written with the dev catalog (freshcart_dev.gold..., freshcart_dev.semantic...), exactly as
# `bundle generate` exports it from dev, and bundles do not substitute variables inside that file. This
# rewrites the catalog to the target's `catalog` variable from databricks.yml. CI runs it on a fresh
# checkout; if you run it locally, don't commit the result (`git checkout src/` undoes it).
#
#     scripts/set_catalog.sh qa
set -euo pipefail

target="${1:?usage: scripts/set_catalog.sh <target>}"
space_file="src/freshcart_assistant.geniespace.json"

catalog="$(databricks bundle validate -t "$target" -o json | jq -r '.variables.catalog.value')"
if [[ -z "$catalog" || "$catalog" == "null" ]]; then
  echo "no catalog variable for target $target" >&2
  exit 1
fi
# (written to a temporary file rather than with sed -i, so it works with GNU and macOS sed alike)
sed -E "s/freshcart_dev\.(gold|semantic)\./${catalog}.\1./g" "$space_file" > "$space_file.tmp"
mv "$space_file.tmp" "$space_file"
echo "Space for target $target now reads catalog $catalog"
