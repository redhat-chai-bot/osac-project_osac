#!/usr/bin/env bash
# Regression test for apply-ca-bundle.sh: verifies that the ingress CA is
# included in the trust-manager Bundle when INCLUDE_INGRESS_CA=true, even
# when USE_DEFAULT_CAS=false (OSAC-5939).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SUT="${SCRIPT_DIR}/../charts/osac-infra/files/hooks/apply-ca-bundle.sh"
TEMPLATE="${SCRIPT_DIR}/../charts/osac-infra/templates/hooks/apply-ca-bundle.yaml"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "${TMP_DIR}"' EXIT

pass=0
fail=0

# Build a mock oc that tees applied YAML.
mkdir -p "${TMP_DIR}/bin"
cat > "${TMP_DIR}/bin/oc" <<'MOCK'
#!/usr/bin/env bash
case "$*" in
  *"get endpoints"*)       echo "10.0.0.1" ;;
  *"get deploy"*)          echo "1" ;;
  *"get certificate"*)     echo "True" ;;
  *"get secret router-ca"*)echo "dGVzdC1pbmdyZXNzLWNhLWRhdGE=" ;;
  *"apply -f -")           tee -a "${APPLIED_YAML_FILE}" > /dev/null ;;
  *)                       : ;;
esac
MOCK
chmod +x "${TMP_DIR}/bin/oc"
MOCK_PATH="${TMP_DIR}/bin:${PATH}"

run_sut() {
  local use_default="$1" include_ingress="$2" out="$3"
  : > "${out}"
  PATH="${MOCK_PATH}" \
  APPLIED_YAML_FILE="${out}" \
  CERT_MANAGER_NAMESPACE=cert-manager \
  WAIT_DEFAULT_CA=false \
  CA_BUNDLE_NAME=ca-bundle \
  RELEASE_NAMESPACE=osac-e2e-ci \
  OSAC_NAMESPACE=osac-e2e-ci \
  CSI_NAMESPACE=osac-csi \
  USE_DEFAULT_CAS="${use_default}" \
  INCLUDE_INGRESS_CA="${include_ingress}" \
  timeout 15 bash "${SUT}" > /dev/null 2>&1 || true
}

# ── Test 1: Both flags false → no ingress CA ──
echo "Test 1: no ingress CA when both flags are false"
run_sut false false "${TMP_DIR}/t1.yaml"
if ! grep -qF "ingress-ca" "${TMP_DIR}/t1.yaml" 2>/dev/null; then
  echo "  PASS: ingress-ca absent"; pass=$((pass + 1))
else
  echo "  FAIL: ingress-ca found"; fail=$((fail + 1))
fi

# ── Test 2: INCLUDE_INGRESS_CA=true → ingress CA included (the fix) ──
echo "Test 2: ingress CA included when INCLUDE_INGRESS_CA=true"
run_sut false true "${TMP_DIR}/t2.yaml"
if grep -qF 'name: "ingress-ca"' "${TMP_DIR}/t2.yaml" 2>/dev/null; then
  echo "  PASS: ingress-ca source in Bundle"; pass=$((pass + 1))
else
  echo "  FAIL: ingress-ca not found"; fail=$((fail + 1))
fi

# ── Test 3: USE_DEFAULT_CAS=true → ingress CA included (legacy) ──
echo "Test 3: ingress CA included when USE_DEFAULT_CAS=true (legacy)"
run_sut true false "${TMP_DIR}/t3.yaml"
if grep -qF 'name: "ingress-ca"' "${TMP_DIR}/t3.yaml" 2>/dev/null; then
  echo "  PASS: ingress-ca in Bundle (legacy)"; pass=$((pass + 1))
else
  echo "  FAIL: ingress-ca not found"; fail=$((fail + 1))
fi

# ── Test 4: Helm template sets INCLUDE_INGRESS_CA from keycloak.enabled ──
echo "Test 4: Helm template sets INCLUDE_INGRESS_CA from keycloak.enabled"
if grep -A1 'INCLUDE_INGRESS_CA' "${TEMPLATE}" | grep -qF '.Values.keycloak.enabled'; then
  echo "  PASS: env references keycloak.enabled"; pass=$((pass + 1))
else
  echo "  FAIL: INCLUDE_INGRESS_CA not derived from keycloak.enabled"; fail=$((fail + 1))
fi

# ── Test 5: RBAC gated on keycloak.enabled only ──
echo "Test 5: RBAC for openshift-ingress-operator gated on keycloak.enabled"
GATE=$(grep -B20 'namespace: openshift-ingress-operator' "${TEMPLATE}" | grep '{{- if' | head -1)
if echo "${GATE}" | grep -qF '.Values.keycloak.enabled'; then
  echo "  PASS: RBAC gate has keycloak.enabled"; pass=$((pass + 1))
else
  echo "  FAIL: RBAC gate missing keycloak.enabled"; fail=$((fail + 1))
fi
if ! echo "${GATE}" | grep -qF 'publicIngress'; then
  echo "  PASS: RBAC gate omits publicIngress"; pass=$((pass + 1))
else
  echo "  FAIL: RBAC gate still references publicIngress"; fail=$((fail + 1))
fi

echo ""
echo "Results: ${pass} passed, ${fail} failed"
if ((fail > 0)); then
  exit 1
fi
