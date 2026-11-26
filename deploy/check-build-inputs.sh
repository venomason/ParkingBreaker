#!/bin/sh
set -eu

case ${PB_EXPECTED_COMMIT:-} in
  ''|*[!0-9a-f]*) valid_revision=false ;;
  *) valid_revision=true ;;
esac
if [ "$valid_revision" != true ] || [ "${#PB_EXPECTED_COMMIT}" -ne 40 ]; then
  echo 'Set _PB_BUILD_COMMIT to the full lowercase Git commit SHA.' >&2
  exit 1
fi

case ${PB_BUILD_IMAGE_TAG:-} in
  ''|staging|latest)
    echo 'Set _IMAGE_TAG to a candidate-specific image tag.' >&2
    exit 1 ;;
esac
