# shellcheck shell=bash
# Google credentials + AI-tool defaults for every interactive workload
# (workspace / Jupyter, VS Code, Linux Desktop). SOURCE it from an entrypoint
# before starting the tools, so every child process inherits the result:
#
#   * GCP_WORKSPACE_JSON (the "Workspace tools & OpenCode" cloud identity,
#     Studio → Cloud identities), falling back to the Vertex AI identity and
#     then GCP_SA_JSON (vault; JSON or base64), is written to RAM-backed
#     /dev/shm/bsp/gcp-adc.json (0600) — never to a disk volume — and exposed
#     as Application Default Credentials: GOOGLE_APPLICATION_CREDENTIALS plus
#     the gcloud well-known ADC path. Both service_account keys and
#     authorized_user sign-ins (gcloud auth application-default login) work:
#     ADC handles either type natively. Vertex AI / BigQuery SDKs, Antigravity
#     (AGY_ADC_AUTH), OpenCode and Claude Code pick it up.
#   * Claude Code on Vertex gets ANTHROPIC_VERTEX_PROJECT_ID + CLOUD_ML_REGION
#     from the same identity. CLAUDE_VERTEX=auto (default) turns
#     CLAUDE_CODE_USE_VERTEX on only when VERTEX_CLAUDE_MODEL names an enabled
#     model; =1 forces Vertex, =0 keeps Claude's own Anthropic sign-in.
#   * Project / region from .env (GCP_PROJECT_ID, GCP_REGION) under the names
#     each tool reads.
#   * OpenCode's global config (~/.config/opencode/opencode.json) is
#     regenerated on every start — appliance-managed; personal settings go in a
#     project-level opencode.json, which OpenCode merges over it.
# With no key configured nothing is written and the tools keep their own
# sign-in flows.

bsp_gcp_adc() {
  local dir=/dev/shm/bsp key="$1"
  local project="${GCP_WORKSPACE_PROJECT:-${GCP_VERTEX_PROJECT:-${GCP_PROJECT_ID:-}}}"
  export GOOGLE_CLOUD_PROJECT="$project" CLOUDSDK_CORE_PROJECT="$project"
  export GOOGLE_CLOUD_QUOTA_PROJECT="$project"
  export GOOGLE_CLOUD_LOCATION="${GCP_REGION:-us-central1}" VERTEX_LOCATION="${GCP_REGION:-us-central1}"
  [[ -n "$key" ]] || return 0
  ( umask 077; mkdir -p "$dir" )
  # JSON or base64 → validated JSON file; exits non-zero (and writes nothing) on garbage
  if ! printf '%s' "$key" | python3 -c '
import base64, json, sys
raw = sys.stdin.read().strip()
info = json.loads(raw if raw.startswith("{") else base64.b64decode(raw))
need = {"service_account": ("client_email", "private_key"),
        "authorized_user": ("client_id", "client_secret", "refresh_token")}.get(info.get("type"))
assert need and all(info.get(k) for k in need), "not a service-account key or authorized_user sign-in"
sys.stdout.write(json.dumps(info))' > "$dir/gcp-adc.json.tmp" 2>/dev/null; then
    rm -f "$dir/gcp-adc.json.tmp"
    echo "[gcp-adc] the Google credential is not a usable service-account key or user sign-in — Google tools keep their own sign-in" >&2
    return 0
  fi
  chmod 600 "$dir/gcp-adc.json.tmp" && mv -f "$dir/gcp-adc.json.tmp" "$dir/gcp-adc.json"
  rm -f "$dir/gcp-sa.json"
  ln -sfn "$dir/gcp-adc.json" "$dir/gcp-sa.json" 2>/dev/null   # old path, for scripts that hard-coded it
  export GOOGLE_APPLICATION_CREDENTIALS="$dir/gcp-adc.json"
  export AGY_ADC_AUTH=true            # Antigravity: use ADC instead of interactive Google sign-in
  # Claude Code: same ADC, same project/region. Forcing Vertex while the
  # project has no Anthropic publisher model just yields 404s, so `auto`
  # waits for VERTEX_CLAUDE_MODEL (an admin confirming the model is enabled).
  export ANTHROPIC_VERTEX_PROJECT_ID="$project"
  export CLOUD_ML_REGION="${CLAUDE_VERTEX_REGION:-${GCP_REGION:-us-central1}}"
  local cv="${CLAUDE_VERTEX:-auto}"
  if [[ "$cv" =~ ^(1|on|true|yes)$ ]] || { [[ "$cv" == auto ]] && [[ -n "${VERTEX_CLAUDE_MODEL:-}" ]]; }; then
    export CLAUDE_CODE_USE_VERTEX=1
  fi
  export GOOGLE_GENAI_USE_VERTEXAI=true   # Google Gen AI SDKs → Vertex, not API key
  # gcloud's well-known ADC path, for tools that ignore GOOGLE_APPLICATION_CREDENTIALS
  mkdir -p "$HOME/.config/gcloud" 2>/dev/null \
    && ln -sfn "$dir/gcp-adc.json" "$HOME/.config/gcloud/application_default_credentials.json" 2>/dev/null
  if [[ -n "${CLAUDE_CODE_USE_VERTEX:-}" ]]; then
    echo "[gcp-adc] Google ADC ready: project ${project:-?}, region ${GCP_REGION:-?} · Claude Code → Vertex (${CLOUD_ML_REGION})" >&2
  else
    echo "[gcp-adc] Google ADC ready: project ${project:-?}, region ${GCP_REGION:-?} · Claude Code keeps its own sign-in" >&2
  fi
}

bsp_opencode_config() {
  local cfg="$HOME/.config/opencode/opencode.json"
  mkdir -p "$(dirname "$cfg")" 2>/dev/null || return 0
  python3 - "$cfg" <<'OPENCODE_PY'
import json, os, socket, sys
from urllib.parse import urlparse


def up(url: str) -> bool:
    u = urlparse(url)
    try:
        socket.create_connection((u.hostname, u.port or 80), timeout=1.5).close()
        return True
    except OSError:
        return False


# Only models verified to answer for this project/region (VERTEX_ALLOWED_MODELS
# in .env). OpenCode's built-in catalogue lists dozens of Vertex models
# (previews, TTS, image) that 404 for most projects, so the managed config
# declares a dedicated `vertex` provider with exactly these and hides the rest.
allowed = [m.strip() for m in os.environ.get(
    "VERTEX_ALLOWED_MODELS", "gemini-2.5-flash,gemini-2.5-pro,gemini-2.5-flash-lite").split(",") if m.strip()]
names = {"gemini-2.5-flash": "Gemini 2.5 Flash", "gemini-2.5-pro": "Gemini 2.5 Pro",
         "gemini-2.5-flash-lite": "Gemini 2.5 Flash-Lite"}
gateway = os.environ.get("LLM_GATEWAY_URL", "http://harness:8096/ai/v1")
providers, enabled = {}, []
if os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") and allowed:
    providers["vertex"] = {
        "npm": "@ai-sdk/google-vertex",
        "name": "Vertex AI (%s)" % (os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("GCP_PROJECT_ID", "")),
        "options": {"project": os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("GCP_PROJECT_ID", ""),
                    "location": os.environ.get("GCP_REGION", "us-central1")},
        "models": {m: {"name": names.get(m, m)} for m in allowed},
    }
    enabled.append("vertex")
if up(gateway):   # the AI harness runs in this appliance: offer its governed aliases too
    providers["bsp-gateway"] = {
        "npm": "@ai-sdk/openai-compatible",
        "name": "DataPlatform LLM gateway",
        "options": {"baseURL": gateway},
        "models": {a: {"name": n} for a, n in [("chat-default", "DataPlatform Chat"),
                                               ("sql-default", "Data Platform SQL"),
                                               ("extract-default", "DataPlatform Extract (JSON)")]},
    }
    enabled.append("bsp-gateway")
cfg = {"$schema": "https://opencode.ai/config.json", "autoupdate": False, "share": "disabled",
       "enabled_providers": enabled, "provider": providers}
if "vertex" in enabled:
    cfg["model"] = "vertex/" + allowed[0]
    cfg["small_model"] = "vertex/" + next((m for m in allowed if "lite" in m), allowed[0])
elif "bsp-gateway" in enabled:
    cfg["model"] = "bsp-gateway/chat-default"
if os.environ.get("MCP_TOKEN"):
    cfg["mcp"] = {"dataplatform": {"type": "remote", "url": os.environ.get("MCP_URL", "http://mcp:8095/mcp"),
                                  "headers": {"Authorization": "Bearer {env:MCP_TOKEN}"}, "enabled": True}}
with open(sys.argv[1], "w") as f:
    json.dump(cfg, f, indent=2)
OPENCODE_PY
}

bsp_gcp_adc "${GCP_WORKSPACE_JSON:-${GCP_VERTEX_JSON:-${GCP_SA_JSON:-}}}"
bsp_opencode_config
