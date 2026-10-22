#!/usr/bin/env bash
# px-proxy end-to-end test with local stand-ins (no real internet needed).
#
#   bash build/px-proxy/tests/e2e.sh            # needs bsp/appliance + curlimages/curl
#
# Topology (mirrors compose behind a TLS-inspecting corporate proxy):
#
#   egtest-net (internal)      egtest-edge             egtest-far ("internet")
#   ┌─────────────┐           ┌──────────────┐        ┌──────────────────────────┐
#   │ client(s)   │──3128──▶  │ egtest-px    │──────▶ │ egtest-upstream (≈ px)   │
#   │ no route out│           │ (uid 1000)   │        │ egtest-inet: pypi.org,   │
#   └─────────────┘           └──────────────┘        │ evil.example.com, :1433  │
#                                                      │ certs signed by a FAKE   │
#                                                      │ "corporate" root │
#                                                      └──────────────────────────┘
# egtest-px can ONLY reach the internet through egtest-upstream (chaining).
# egtest-px-direct (AKS shape) sits on egtest-far directly.
# Everything runs as uid 1000. All egtest-* resources are removed on exit.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
PX="${REPO}/build/px-proxy"
IMG="${EGTEST_IMAGE:-bsp/appliance:latest}"
CURL_IMG="${EGTEST_CURL_IMAGE:-curlimages/curl:latest}"
W="${EGTEST_WORKDIR:-$(mktemp -d)}"
mkdir -p "${W}"/{corp,certs,data,data2,corp2}
PASS=0; FAIL=0; RESULTS=()

cleanup() {
  docker rm -f egtest-px egtest-px-direct egtest-inet egtest-upstream egtest-client >/dev/null 2>&1
  docker network rm egtest-net egtest-edge egtest-far >/dev/null 2>&1
  docker volume rm egtest-trust egtest-ca egtest-trust2 egtest-ca2 >/dev/null 2>&1
}
[[ "${EGTEST_KEEP:-0}" == "1" ]] || trap cleanup EXIT
cleanup

ok()   { PASS=$((PASS+1)); RESULTS+=("PASS  $1"); echo "  PASS  $1"; }
bad()  { FAIL=$((FAIL+1)); RESULTS+=("FAIL  $1 :: $2"); echo "  FAIL  $1 :: $2"; }
expect() {  # name, haystack, needle
  if grep -qF -- "$3" <<<"$2"; then ok "$1"; else bad "$1" "expected '$3' in: $(head -c 400 <<<"$2")"; fi
}
expect_not() {
  if grep -qF -- "$3" <<<"$2"; then bad "$1" "did not expect '$3' in: $(head -c 400 <<<"$2")"; else ok "$1"; fi
}
curlc() {  # curl from a throwaway client on the internal network only
  docker run --rm --network egtest-net -v egtest-trust:/trust:ro --user 1000:1000 "${CURL_IMG}" \
    --max-time 15 -sS "$@" 2>&1
}
admin() {  # method path [json]
  local tok; tok="$(tr -d '\n' < "${W}/data/.admin-token")"
  docker exec egtest-px curl -sS -X "$1" -H "Authorization: Bearer ${tok}" -H 'Content-Type: application/json' \
    "http://127.0.0.1:8081$2" ${3:+-d "$3"}
}
vol_uid1000() { docker volume create --driver local --opt type=tmpfs --opt device=tmpfs --opt o=uid=1000,gid=1000,mode=0755 "$1" >/dev/null; }
wait_ready() {
  for _ in $(seq 1 60); do docker exec "$1" test -f /trust/ready 2>/dev/null && return 0; sleep 1; done
  docker logs "$1" | tail -30; return 1
}

echo "==> setup (workdir ${W})"
docker network create --internal egtest-net >/dev/null
docker network create egtest-edge >/dev/null
docker network create egtest-far >/dev/null
vol_uid1000 egtest-trust; vol_uid1000 egtest-ca; vol_uid1000 egtest-trust2; vol_uid1000 egtest-ca2

docker run --rm --user 1000:1000 --entrypoint python -v "${PX}/tests/fixtures:/fx:ro" -v "${W}/certs:/out" \
  "${IMG}" /fx/make_certs.py /out >/dev/null

docker run -d --name egtest-inet --user 1000:1000 --network egtest-far \
  --network-alias pypi.org --network-alias files.pythonhosted.org --network-alias evil.example.com \
  --network-alias notallowed.example.net --network-alias sql01.dataplatform.com --network-alias api.dataplatform.com \
  -v "${PX}/tests/fixtures:/fx:ro" -v "${W}/certs:/certs:ro" --entrypoint python "${IMG}" -u /fx/fake_internet.py /certs >/dev/null

docker run -d --name egtest-upstream --user 1000:1000 --network egtest-edge \
  -v "${PX}/tests/fixtures:/fx:ro" --entrypoint python "${IMG}" -u /fx/fake_upstream_proxy.py 3128 >/dev/null
docker network connect egtest-far egtest-upstream

# px-proxy: chained to the upstream stand-in; corporate dir starts EMPTY.
docker run -d --name egtest-px --user 1000:1000 --network egtest-net \
  -e EGRESS_UPSTREAM_PROXY=http://egtest-upstream:3128 -e EGRESS_MODE=enforce \
  -e EGRESS_ALLOWLIST=/data/egress/allowlist.yaml -e EGRESS_DECISIONS=/data/egress/decisions.jsonl \
  -e CORPORATE_CA_DIR=/certs/corporate -e EGRESS_CORP_RESCAN_S=4 \
  -v "${PX}:/opt/px-proxy:ro" -v "${W}/corp:/certs/corporate:ro" -v "${W}/data:/data/egress" \
  -v egtest-trust:/trust -v egtest-ca:/home/analyst/.mitmproxy \
  --entrypoint python "${IMG}" /opt/px-proxy/run.py >/dev/null
docker network connect egtest-edge egtest-px

# a long-lived appliance-image client with the compose x-trust-env
docker run -d --name egtest-client --user 1000:1000 --network egtest-net -v egtest-trust:/trust:ro \
  -v "${PX}:/opt/px-proxy:ro" \
  -e HTTP_PROXY=http://egtest-px:3128 -e HTTPS_PROXY=http://egtest-px:3128 \
  -e http_proxy=http://egtest-px:3128 -e https_proxy=http://egtest-px:3128 -e NO_PROXY=localhost,127.0.0.1 \
  -e SSL_CERT_FILE=/trust/ca-bundle.pem -e REQUESTS_CA_BUNDLE=/trust/ca-bundle.pem \
  --entrypoint sleep "${IMG}" infinity >/dev/null

wait_ready egtest-px || { echo "px-proxy never became ready"; exit 1; }
echo "==> px-proxy ready"

echo "==> 0. runs as non-root"
expect "px-proxy uid is 1000" "$(docker exec egtest-px sh -c 'id -u; ps -o user= -p 1')" "1000"
expect "trust files present" "$(docker exec egtest-px ls /trust)" "ca-bundle.pem"

echo "==> 1. allowed host through the chain (corporate root hot-reload)"
out="$(curlc --cacert /trust/ca-bundle.pem -x http://egtest-px:3128 -o /dev/null -w '%{http_code}' https://pypi.org/simple/)"
expect "1a without corporate root → upstream TLS verify fails (502)" "${out}" "502"
cp "${W}/certs/fake-corporate-root.pem" "${W}/corp/"
sleep 7
out="$(curlc --cacert /trust/ca-bundle.pem -x http://egtest-px:3128 https://pypi.org/simple/)"
expect "1b corporate root picked up without restart → 200 from fake pypi.org" "${out}" '"fake_internet": true'
expect "1c chained through upstream proxy" "$(docker logs egtest-upstream 2>&1)" "CONNECT pypi.org:443"
out="$(docker exec egtest-client python /opt/px-proxy/tests/fixtures/client_probe.py requests https://files.pythonhosted.org/packages/x)"
expect "1d python-requests with /trust/ca-bundle.pem" "${out}" "200"
out="$(curlc -x http://egtest-px:3128 -o /dev/null -w '%{http_code}' https://pypi.org/simple/)"
expect "1e client WITHOUT the egress CA gets a TLS verify error (curl 60)" "${out}" "curl: (60)"

echo "==> 2. disallowed host"
out="$(curlc --cacert /trust/ca-bundle.pem -x http://egtest-px:3128 -v https://evil.example.com/ 2>&1)"
expect "2a HTTPS CONNECT refused 403" "${out}" "403 Blocked by appliance egress policy"
out="$(curlc -x http://egtest-px:3128 -H 'Accept: text/plain' http://evil.example.com/x -w ' [%{http_code}]')"
expect "2b plain HTTP refused 403" "${out}" "[403]"
expect "2c message points to Studio" "${out}" "Studio → Egress"
out="$(curlc --cacert /trust/ca-bundle.pem -x http://egtest-px:3128 -o /dev/null -w '%{http_code}' https://pypi.org:8443/)"
expect "2d allowed host on a non-allowed port refused" "${out}" "403"

echo "==> 3. no direct egress from the internal network"
out="$(curlc --noproxy '*' --max-time 5 https://pypi.org/ ; echo "rc=$?")"
expect "3a direct (no proxy) cannot even resolve names" "${out}" "Resolv"
expect_not "3a' direct (no proxy) gets no data" "${out}" "fake_internet"
INET_IP="$(docker inspect -f '{{(index .NetworkSettings.Networks "egtest-far").IPAddress}}' egtest-inet)"
out="$(curlc --noproxy '*' --max-time 5 -k "https://${INET_IP}/" ; echo "rc=$?")"
expect_not "3b direct to internet IP ${INET_IP} fails" "${out}" "fake_internet"

echo "==> 5. hot reload"
out="$(admin POST /rules '{"rules":[{"pattern":"evil.example.com","category":"Test","reason":"e2e"}]}')"
expect "5a admin API upsert" "${out}" '"added": ["evil.example.com"]'
out="$(curlc --cacert /trust/ca-bundle.pem -x http://egtest-px:3128 https://evil.example.com/)"
expect "5b newly allowed host works immediately" "${out}" '"fake_internet": true'
sed -i.bak 's/evil\.example\.com/evil-renamed.example.com/' "${W}/data/allowlist.yaml"
sleep 3
out="$(curlc --cacert /trust/ca-bundle.pem -x http://egtest-px:3128 -o /dev/null -w '%{http_code}' https://evil.example.com/)"
expect "5c file edit picked up (mtime) → blocked again" "${out}" "403"
printf 'mode: enforce\nrules: [this is not: valid\n' > "${W}/data/allowlist.yaml.broken"
cp "${W}/data/allowlist.yaml" "${W}/data/allowlist.yaml.good"
cp "${W}/data/allowlist.yaml.broken" "${W}/data/allowlist.yaml"; sleep 3
out="$(curlc --cacert /trust/ca-bundle.pem -x http://egtest-px:3128 -o /dev/null -w '%{http_code}' https://pypi.org/simple/)"
expect "5d broken YAML keeps the last good policy" "${out}" "200"
cp "${W}/data/allowlist.yaml.good" "${W}/data/allowlist.yaml"; sleep 3

echo "==> 6. audit mode"
pol="$(admin GET /policy | python3 -c 'import json,sys; d=json.load(sys.stdin); p=d["policy"]; p["mode"]="audit"; print(json.dumps({"policy":p}))')"
out="$(admin PUT /policy "${pol}")"
expect "6a switch to audit" "${out}" '"mode": "audit"'
out="$(curlc --cacert /trust/ca-bundle.pem -x http://egtest-px:3128 https://notallowed.example.net/)"
expect "6b non-allowlisted host passes in audit" "${out}" '"fake_internet": true'
out="$(admin GET '/decisions?limit=20&host=notallowed')"
expect "6c logged as would-block" "${out}" '"would_block": true'
pol="$(admin GET /policy | python3 -c 'import json,sys; d=json.load(sys.stdin); p=d["policy"]; p["mode"]="enforce"; print(json.dumps({"policy":p}))')"
admin PUT /policy "${pol}" >/dev/null
out="$(curlc --cacert /trust/ca-bundle.pem -x http://egtest-px:3128 -o /dev/null -w '%{http_code}' https://notallowed.example.net/)"
expect "6d back to enforce → 403" "${out}" "403"

echo "==> 7. TCP tunnels"
pol="$(admin GET /policy | python3 -c 'import json,sys; d=json.load(sys.stdin); p=d["policy"]; p["tcp_tunnels"]=[{"name":"dwh","listen_port":14330,"target_host":"sql01.dataplatform.com","target_port":1433,"reason":"e2e"},{"name":"bad","listen_port":14331,"target_host":"evil.example.com","target_port":1433,"reason":"e2e"}]; print(json.dumps({"policy":p}))')"
admin PUT /policy "${pol}" >/dev/null; sleep 2
out="$(docker exec egtest-client python /opt/px-proxy/tests/fixtures/client_probe.py tunnel egtest-px 14330 hello)"
expect "7a tunnel to allowed *.dataplatform.com:1433 works (via upstream CONNECT)" "${out}" "echo:hello"
out="$(docker exec egtest-client python /opt/px-proxy/tests/fixtures/client_probe.py tunnel egtest-px 14331 hello)"
expect "7b tunnel to non-allowed target is closed" "${out}" "CLOSED"
expect "7c tunnel status via admin" "$(admin GET /tunnels)" '"target_allowed": false'

echo "==> 8. SNI / Host-header smuggling"
out="$(docker exec egtest-client python /opt/px-proxy/tests/fixtures/client_probe.py sni egtest-px pypi.org evil.example.com 2>&1)"
expect "8a CONNECT allowed host + SNI of blocked host → 403" "${out}" "GET -> HTTP/1.1 403"
expect "8b reason sni-mismatch" "${out}" "sni-mismatch"
out="$(curlc --cacert /trust/ca-bundle.pem -x http://egtest-px:3128 -H 'Host: evil.example.com' -o /dev/null -w '%{http_code}' https://pypi.org/)"
expect "8c Host header of a blocked host → 403" "${out}" "403"

echo "==> 9. decision log + stats"
out="$(tail -5 "${W}/data/decisions.jsonl")"
expect "9a JSONL written" "${out}" '"action"'
out="$(admin GET '/stats?hours=24')"
expect "9b stats aggregate top blocked" "${out}" '"top_blocked"'
expect "9c admin API rejects missing token" "$(docker exec egtest-px curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8081/policy)" "401"
expect "9d admin test endpoint" "$(admin POST /test '{"url":"https://cdn-lfs-us-1.huggingface.co/x"}')" '"allowed": true'

echo "==> 10. direct mode, AKS shape: no upstream, TLS passthrough, GitOps read-only allowlist"
cp "${W}/certs/fake-corporate-root.pem" "${W}/corp2/"
cp "${PX}/allowlist.default.yaml" "${W}/data2/allowlist.yaml"
docker run -d --name egtest-px-direct --user 1000:1000 --network egtest-net \
  -e EGRESS_ALLOWLIST=/data/egress/allowlist.yaml -e EGRESS_DECISIONS=/data/egress/decisions.jsonl \
  -e CORPORATE_CA_DIR=/certs/corporate -e EGRESS_TLS_INSPECT=false -e EGRESS_POLICY_READONLY=true \
  -e EGRESS_ALLOW_AUDIT=false -e EGRESS_DECISIONS_STDOUT=true \
  -v "${PX}:/opt/px-proxy:ro" -v "${W}/corp2:/certs/corporate:ro" -v "${W}/data2:/data/egress" \
  -v egtest-trust2:/trust -v egtest-ca2:/home/analyst/.mitmproxy \
  --entrypoint python "${IMG}" /opt/px-proxy/run.py >/dev/null
docker network connect egtest-far egtest-px-direct
wait_ready egtest-px-direct || bad "10 direct px ready" "not ready"
dcurl() { docker run --rm --network egtest-net -v "${W}/certs:/certs:ro" --user 1000:1000 "${CURL_IMG}" --max-time 15 -sS "$@" 2>&1; }
out="$(dcurl --cacert /certs/fake-corporate-root.pem -x http://egtest-px-direct:3128 https://pypi.org/simple/)"
expect "10a passthrough: client sees the ORIGIN cert (no egress CA needed) → 200" "${out}" '"fake_internet": true'
out="$(dcurl --cacert /certs/fake-corporate-root.pem -x http://egtest-px-direct:3128 -o /dev/null -w '%{http_code}' https://evil.example.com/)"
expect "10b passthrough: blocked host → 403 at CONNECT" "${out}" "403"
out="$(docker exec egtest-px-direct sh -c 'python - <<PY
import socket
s = socket.create_connection(("127.0.0.1", 3128))
s.sendall(b"CONNECT pypi.org:443 HTTP/1.1\\r\\nHost: pypi.org:443\\r\\n\\r\\n")
print(s.recv(200).split(b"\\r\\n")[0].decode())
import ssl
ctx = ssl.create_default_context(cafile="/certs/corporate/fake-corporate-root.pem")
try:
    t = ctx.wrap_socket(s, server_hostname="evil.example.com"); print("HANDSHAKE-OK")
except Exception as e:
    print("HANDSHAKE-FAILED", type(e).__name__)
PY')"
expect "10c passthrough: SNI smuggling is not passed through" "${out}" "HANDSHAKE-FAILED"
tok2="$(tr -d '\n' < "${W}/data2/.admin-token")"
out="$(docker exec egtest-px-direct curl -sS -X POST -H "Authorization: Bearer ${tok2}" -H 'Content-Type: application/json' http://127.0.0.1:8081/rules -d '{"rules":[{"pattern":"x.example.com"}]}')"
expect "10d GitOps read-only allowlist refuses API writes (409)" "${out}" '"read_only"'
expect "10e decisions also on stdout (Log Analytics)" "$(docker logs egtest-px-direct 2>&1)" 'EGRESS {'
out="$(docker run --rm --user 1000:1000 --network none -v egtest-trust:/src:ro -v "${PX}:/opt/px-proxy:ro" --entrypoint bash "${IMG}" \
  -c 'python /opt/px-proxy/run.py --trust-only --ca-cert /src/egress-ca.pem --trust-dir /tmp/t >/dev/null && ls /tmp/t')"
expect "10f init-container mode writes /trust from the public cert only" "${out}" "ready"

echo
echo "================ px-proxy e2e: ${PASS} passed, ${FAIL} failed ================"
printf '%s\n' "${RESULTS[@]}"
[[ "${FAIL}" -eq 0 ]]
