#!/usr/bin/env bash
# Extract corporate TLS-interception root CAs (Zscaler, Netskope, …) from this host's
# trust store into build/certs/corporate/ for px-proxy (plan §2).
#
#   make corporate-ca            # = bash build/scripts/extract-corporate-ca.sh
#
# Run on the HOST (Mac or Linux), never inside the appliance. Read-only with
# respect to the keychain. px-proxy picks the new files up within ~30 s (no
# restart): it re-reads CORPORATE_CA_DIR and rebuilds its upstream trust.
# Clients never need these roots — they trust only the appliance egress CA.
#
# Each certificate is decoded and matched on its SUBJECT / ISSUER.
#
# Env knobs
#   CORP_CA_PATTERN   case-insensitive ERE matched against subject/issuer
#                     (default: Zscaler|Netskope|DataPlatform|Forcepoint|Blue ?Coat|Palo Alto)
#   EXTRA_CA_FILES    space-separated extra PEM files to include
#   INCLUDE_LOGIN_KEYCHAIN=1   also scan ~/Library/Keychains/login.keychain-db
#
# Output
#   build/certs/corporate/<slug>.pem   one file per root (header comments + PEM)
#   build/certs/corporate-root.crt       compat: all extracted roots concatenated
#                                      (legacy Dockerfile COPY; empty if none)
#   build/certs/corporate-root.pem       same content (legacy name)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CERTS="${ROOT}/build/certs"
OUT_DIR="${CERTS}/corporate"
PATTERN="${CORP_CA_PATTERN:-Zscaler|Netskope|DataPlatform|Forcepoint|Blue ?Coat|Palo Alto}"
MARK="# extracted-by: build/scripts/extract-corporate-ca.sh"

command -v openssl >/dev/null || { echo "!! openssl not found" >&2; exit 1; }
mkdir -p "${OUT_DIR}"
WORK="$(mktemp -d)"; trap 'rm -rf "${WORK}"' EXIT

collect() {  # $1 = label, stdin = PEM stream → split into WORK/src-N.pem
  local label="$1"
  awk -v dir="${WORK}" -v label="${label}" '
    /-----BEGIN CERTIFICATE-----/ { n++; f = sprintf("%s/cand-%s-%04d.pem", dir, label, n); inside=1 }
    inside { print > f }
    /-----END CERTIFICATE-----/ { inside=0; close(f) }'
}

echo "==> Scanning trust stores for corporate roots (pattern: ${PATTERN})"
if command -v security >/dev/null 2>&1; then
  for kc in /Library/Keychains/System.keychain /System/Library/Keychains/SystemRootCertificates.keychain; do
    [[ -f "${kc}" ]] || continue
    security find-certificate -a -p "${kc}" 2>/dev/null | collect "$(basename "${kc}" .keychain)" || true
  done
  if [[ "${INCLUDE_LOGIN_KEYCHAIN:-0}" == "1" && -f "${HOME}/Library/Keychains/login.keychain-db" ]]; then
    security find-certificate -a -p "${HOME}/Library/Keychains/login.keychain-db" 2>/dev/null | collect login || true
  fi
fi
for f in "/Library/Application Support/Zscaler/zscaler-root-ca.pem" \
         "${HOME}/Library/Application Support/Zscaler/zscaler-root-ca.pem" \
         /usr/local/share/ca-certificates/*.crt /etc/pki/ca-trust/source/anchors/*.pem \
         ${EXTRA_CA_FILES:-}; do
  [[ -f "${f}" ]] && collect file < "${f}" || true
done

# ── select, verify, dedupe ───────────────────────────────────────────────
: > "${WORK}/seen"
: > "${WORK}/selected"
shopt -s nullglob
for c in "${WORK}"/cand-*.pem; do
  subj="$(openssl x509 -in "${c}" -noout -subject 2>/dev/null | sed 's/^subject= *//')" || continue
  [[ -n "${subj}" ]] || continue
  issuer="$(openssl x509 -in "${c}" -noout -issuer 2>/dev/null | sed 's/^issuer= *//')"
  echo "${subj} ${issuer}" | grep -Eiq "${PATTERN}" || continue
  if ! openssl x509 -in "${c}" -noout -text 2>/dev/null | grep -q "CA:TRUE"; then
    echo "   skip (not a CA): ${subj}"; continue
  fi
  fp="$(openssl x509 -in "${c}" -noout -fingerprint -sha256 | sed 's/.*=//')"
  grep -qx "${fp}" "${WORK}/seen" && continue
  echo "${fp}" >> "${WORK}/seen"
  if ! openssl x509 -in "${c}" -noout -checkend 0 >/dev/null 2>&1; then
    echo "   skip (EXPIRED): ${subj}"; continue
  fi
  echo "${c}" >> "${WORK}/selected"
done

# Remove files this script generated previously (hand-added PEMs are kept).
for old in "${OUT_DIR}"/*.pem; do
  head -1 "${old}" | grep -qF "${MARK}" && rm -f "${old}"
done

COUNT=0
: > "${WORK}/combined"
while IFS= read -r c; do
  [[ -n "${c}" ]] || continue
  cn="$(openssl x509 -in "${c}" -noout -subject | sed -n 's/.*CN *= *\([^,/]*\).*/\1/p')"
  [[ -n "${cn}" ]] || cn="corporate-root-$((COUNT + 1))"
  slug="$(echo "${cn}" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9]+/-/g; s/^-|-$//g')"
  [[ "${slug}" == "corporate-root-ca" ]] && slug="corporate-root"
  dest="${OUT_DIR}/${slug}.pem"
  [[ -e "${dest}" ]] && dest="${OUT_DIR}/${slug}-$((COUNT + 1)).pem"
  {
    echo "${MARK}"
    echo "# subject: $(openssl x509 -in "${c}" -noout -subject | sed 's/^subject= *//')"
    echo "# $(openssl x509 -in "${c}" -noout -enddate)"
    echo "# sha256: $(openssl x509 -in "${c}" -noout -fingerprint -sha256 | sed 's/.*=//')"
    openssl x509 -in "${c}" -outform PEM
  } > "${dest}"
  openssl x509 -in "${c}" -outform PEM >> "${WORK}/combined"
  COUNT=$((COUNT + 1))
  echo "==> ${dest#${ROOT}/}"
  openssl x509 -in "${dest}" -noout -subject -issuer -enddate | sed 's/^/      /'
  echo "      sha256 $(openssl x509 -in "${dest}" -noout -fingerprint -sha256 | sed 's/.*=//')"
done < "${WORK}/selected"

cp "${WORK}/combined" "${CERTS}/corporate-root.crt"
cp "${WORK}/combined" "${CERTS}/corporate-root.pem"

if [[ "${COUNT}" -eq 0 ]]; then
  echo "!! No corporate root CA found. Fine on a network without TLS interception"
  echo "   (and on AKS). On a TLS-inspecting corporate network, check: security find-certificate -a -c Zscaler /Library/Keychains/System.keychain"
  echo "   Wrote empty compat file build/certs/corporate-root.crt (legacy image build still works)."
else
  # sanity: every staged file must parse
  for f in "${OUT_DIR}"/*.pem; do openssl x509 -in "${f}" -noout >/dev/null || { echo "!! ${f} is not a valid PEM" >&2; exit 1; }; done
  echo "==> ${COUNT} corporate root(s) staged in build/certs/corporate/ (+ compat build/certs/corporate-root.crt)"
  echo "    px-proxy reloads them automatically within ~30 s; no client restart needed."
fi
