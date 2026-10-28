#!/bin/sh
# Run the committed suites with Docker alone. No .env, host Python/Node,
# existing services, mounted state, exposed ports, or provider network is used.
set -eu

project_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
test_image=${HAMSAFAR_TEST_IMAGE:-hamsafar-demo-tests:local}
ui_test_image=${HAMSAFAR_UI_TEST_IMAGE:-hamsafar-demo-ui-tests:local}

docker build --target tests --tag "$test_image" "$project_root"
docker run --rm --network none --read-only \
  --tmpfs /tmp:rw,nosuid,nodev,size=512m \
  "$test_image" python -m pytest -q -p no:cacheprovider "$@"

# The existing frontend build target contains source and locked dependencies;
# its production build also runs here without adding tests to the Nginx image.
docker build --target build --tag "$ui_test_image" "$project_root/frontend"
docker run --rm --network none --read-only --entrypoint sh \
  "$ui_test_image" -c 'node --test src/*.test.js'
