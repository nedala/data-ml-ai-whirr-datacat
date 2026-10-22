#!/usr/bin/env bash
# with-build-trust CMD… — run an image-build step that downloads things,
# trusting an OPTIONAL extra CA for this step only (e.g. a corporate TLS-
# inspecting proxy on the build host). Nothing is baked into the image:
#
#   docker build --secret id=build_ca,src=build/certs/corporate/corporate-root.pem \
#                --build-arg HTTPS_PROXY=http://host.docker.internal:3128 .
#
# The Dockerfile mounts the secret per RUN (`--mount=type=secret,id=build_ca`);
# when absent this is a plain exec. At RUNTIME trust comes from /trust
# (px-proxy), never from the image.
set -euo pipefail
CA=/run/secrets/build_ca
if [[ -s "${CA}" ]]; then
  BUNDLE="$(mktemp /tmp/build-ca-bundle.XXXXXX.pem)"
  cat /etc/ssl/certs/ca-certificates.crt "${CA}" > "${BUNDLE}"
  export SSL_CERT_FILE="${BUNDLE}" REQUESTS_CA_BUNDLE="${BUNDLE}" CURL_CA_BUNDLE="${BUNDLE}" \
         PIP_CERT="${BUNDLE}" NODE_EXTRA_CA_CERTS="${CA}" \
         GIT_SSL_CAINFO="${BUNDLE}"
  trap 'rm -f "${BUNDLE}"' EXIT
  "$@"
  exit $?
fi
exec "$@"
