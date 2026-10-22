"""Mock SAP BRIM system, auto-mounted by the connectors app at /connectors/sap_brim/.

(a) OData V4 service mirroring "Contract Accounting Dispute Case"
    <base>/sap/opu/odata4/sap/api_cadisputecase/srvd_a2x/sap/cadisputecase/0002/
      GET  /            service document (send `X-CSRF-Token: Fetch` to get a token)
      GET  /$metadata   EDMX
      GET  /DisputeCase[?$filter=Prop eq 'v'&$top=n]
      GET  /DisputeCase('<id>')
      POST /DisputeCase (basic auth + X-CSRF-Token; deep insert _DisputedObject/_Note)
(b) A dispute-entry web UI at <base>/ui/ — login, account search, account
    overview, 3-step "Create Dispute Case", confirmation — with realistic
    latency and per-render element ids, for the Playwright adapter.
Any non-empty user/password is accepted (it is a mock).
"""

from __future__ import annotations

import asyncio
import base64
import html
import json
import re
import secrets
import time

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from . import mock_backend as mb
from .config import field_map

router = APIRouter(tags=["sap_brim"])
ALIASES = ["sap"]  # architecture §4 also names /connectors/sap/ui/
SVC = field_map()["service_path"]
_tokens: dict[str, str] = {}
_sessions: dict[str, str] = {}
_drafts: dict[str, dict] = {}


@router.get("/")
def index(request: Request):
    base = str(request.url).rstrip("/")
    return {"system": "SAP BRIM (mock)", "odata_service": base + SVC + "/", "metadata": base + SVC + "/$metadata",
            "web_ui": base + "/ui/", "disputes": len(mb.load()["disputes"])}


@router.get("/api/disputes")
def list_disputes(limit: int = 200):
    rows = sorted(mb.load()["disputes"].values(), key=lambda d: d["CaseID"], reverse=True)
    return {"count": len(rows), "value": rows[:limit]}


# ── OData ───────────────────────────────────────────────────────────────
def _odata_err(status: int, code: str, msg: str, headers: dict | None = None) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": msg}}, status_code=status, headers=headers)


def _basic_user(request: Request) -> str | None:
    h = request.headers.get("authorization", "")
    if not h.lower().startswith("basic "):
        return None
    try:
        user, _, pwd = base64.b64decode(h[6:]).decode().partition(":")
    except Exception:
        return None
    return user if user and pwd else None


def _filter(rows: list[dict], expr: str | None) -> list[dict]:
    if not expr:
        return rows
    conds = re.findall(r"(\w+)\s+eq\s+'((?:[^']|'')*)'", expr)
    if not conds:
        raise ValueError(f"unsupported $filter: {expr}")
    return [r for r in rows if all(str(r.get(k)) == v.replace("''", "'") for k, v in conds)]


def _entity(d: dict, ctx: str) -> dict:
    return {"@odata.context": ctx, **d}


@router.api_route(SVC + "/{rest:path}", methods=["GET", "POST", "HEAD"])
async def odata(rest: str, request: Request):
    user = _basic_user(request)
    if not user:
        return _odata_err(401, "/IWFND/MED/170", "Logon failed (basic authentication required)",
                          {"WWW-Authenticate": 'Basic realm="SAP NetWeaver Application Server [MCK/100]"'})
    ctx = str(request.url).split(SVC)[0] + SVC + "/$metadata"
    hdr = {"OData-Version": "4.0"}
    if request.headers.get("x-csrf-token", "").lower() == "fetch":
        _tokens[user] = _tokens.get(user) or secrets.token_urlsafe(18)
        hdr["x-csrf-token"] = _tokens[user]
    rest = rest.strip("/")
    if request.method in ("GET", "HEAD"):
        if rest == "":
            return JSONResponse({"@odata.context": "$metadata", "value": [
                {"name": "DisputeCase", "kind": "EntitySet", "url": "DisputeCase"}]}, headers=hdr)
        if rest == "$metadata":
            return Response(mb.metadata_xml(), media_type="application/xml", headers=hdr)
        if rest == "DisputeCase":
            try:
                rows = _filter(list(mb.load()["disputes"].values()), request.query_params.get("$filter"))
            except ValueError as e:
                return _odata_err(400, "BAD_FILTER", str(e))
            top = int(request.query_params.get("$top", "100"))
            return JSONResponse({"@odata.context": ctx + "#DisputeCase", "value": rows[:top]}, headers=hdr)
        m = re.fullmatch(r"DisputeCase\((?:CaseID=)?'([^']+)'\)", rest)
        if m:
            d = mb.load()["disputes"].get(m.group(1))
            if not d:
                return _odata_err(404, "NOT_FOUND", f"Dispute case {m.group(1)} does not exist")
            return JSONResponse(_entity(d, ctx + "#DisputeCase/$entity"), headers=hdr)
        return _odata_err(404, "NOT_FOUND", f"Resource {rest} not found")
    # POST
    if rest != "DisputeCase":
        return _odata_err(405, "METHOD", f"POST not allowed on {rest}")
    tok = request.headers.get("x-csrf-token", "")
    if not tok or tok != _tokens.get(user):
        return _odata_err(403, "CSRF", "CSRF token validation failed", {"x-csrf-token": "Required"})
    try:
        body = await request.json()
    except Exception:
        return _odata_err(400, "BAD_JSON", "request body is not JSON")
    unknown = [k for k in body if k not in {p for p, _, _ in mb.PROPS} | {"_DisputedObject", "_Note"}]
    if unknown:
        return _odata_err(400, "UNKNOWN_PROPERTY", f"Property {unknown[0]} is not defined for DisputeCaseType")
    await asyncio.sleep(mb.latency(0.15, 0.6))
    try:
        d = mb.create(body, user, "ODATA")
    except mb.MockError as e:
        return _odata_err(400 if "FDM_DUPLICATE" not in str(e) else 409, "FDM", str(e))
    return JSONResponse(_entity(d, ctx + "#DisputeCase/$entity"), status_code=201,
                        headers={**hdr, "Location": f"DisputeCase('{d['CaseID']}')"})


# ── web UI ──────────────────────────────────────────────────────────────
def _base(request: Request) -> str:
    p = request.url.path
    return p[: p.index("/ui")] if "/ui" in p else p.rstrip("/")


def _rid(name: str) -> str:
    return f"__xmlview{secrets.randbelow(90) + 10}--{name}-{secrets.token_hex(3)}"


def _e(s) -> str:
    return html.escape("" if s is None else str(s))


CSS = """
*{box-sizing:border-box}body{margin:0;font:14px/1.45 Arial,Helvetica,sans-serif;background:#edeff0;color:#32363a}
header{background:#354a5f;color:#fff;padding:10px 18px;display:flex;align-items:center;gap:18px}
header .t{font-weight:bold;font-size:15px}header .u{margin-left:auto;font-size:12px;opacity:.85}
header a{color:#fff}main{max-width:1000px;margin:18px auto;padding:0 16px}
.panel{background:#fff;border:1px solid #d9d9d9;border-radius:4px;padding:16px 20px;margin-bottom:14px}
h1{font-size:18px;margin:0 0 10px;font-weight:normal}h2{font-size:15px;margin:0 0 8px}
label{display:block;font-size:12px;color:#6a6d70;margin:10px 0 3px}
input,select,textarea{width:100%;max-width:420px;padding:6px 8px;border:1px solid #89919a;border-radius:3px;font:inherit;background:#fff}
textarea{max-width:100%;height:90px}button,.btn{background:#0a6ed1;color:#fff;border:0;border-radius:3px;padding:7px 14px;font:inherit;cursor:pointer;text-decoration:none;display:inline-block}
button.sec{background:#fff;color:#0a6ed1;border:1px solid #0a6ed1}button[disabled]{background:#9bb9d8;cursor:wait}
table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:6px 8px;border-bottom:1px solid #e5e5e5;font-size:13px}
th{color:#6a6d70;font-weight:normal;background:#f7f7f7}.msg{padding:10px 12px;border-radius:3px;margin-bottom:12px}
.err{background:#ffebeb;border-left:4px solid #bb0000}.ok{background:#f1fdf6;border-left:4px solid #107e3e}
.steps{display:flex;gap:6px;margin-bottom:12px}.steps span{padding:4px 10px;border-radius:12px;background:#e5e5e5;font-size:12px}
.steps span.on{background:#0a6ed1;color:#fff}.busy{color:#6a6d70;padding:24px 0}.kv td:first-child{color:#6a6d70;width:200px}
.row{display:flex;gap:10px;margin-top:16px}
"""


def _page(request: Request, title: str, body: str, user: str | None = None) -> HTMLResponse:
    b = _base(request)
    u = f'<span class="u">User: {_e(user)} &middot; <a href="{b}/ui/logout">Log off</a></span>' if user else ""
    return HTMLResponse(f"""<!doctype html><html><head><meta charset="utf-8"><title>{_e(title)} - SAP BRIM (mock)</title>
<style>{CSS}</style></head><body><header><span class="t">SAP BRIM &middot; Dispute Management (mock system)</span>
<a href="{b}/ui/home">Home</a>{u}</header><main>{body}</main>
<script>
// realistic UI latency: content marked data-lazy appears after a short busy phase
document.querySelectorAll('[data-lazy]').forEach(function(el){{
  var busy=document.createElement('div');busy.className='busy';busy.textContent='Loading…';
  el.style.display='none';el.parentNode.insertBefore(busy,el);
  setTimeout(function(){{busy.remove();el.style.display='';}}, 250+Math.random()*650);
}});
</script></body></html>""")


def _user(request: Request) -> str | None:
    return _sessions.get(request.cookies.get("brim_sid", ""))


def _need_login(request: Request) -> RedirectResponse:
    return RedirectResponse(f"{_base(request)}/ui/login", status_code=303)


@router.get("/ui/", include_in_schema=False)
@router.get("/ui", include_in_schema=False)
def ui_root(request: Request):
    return RedirectResponse(f"{_base(request)}/ui/{'home' if _user(request) else 'login'}", status_code=303)


@router.get("/ui/login", include_in_schema=False)
def ui_login(request: Request, error: str = ""):
    u, p = _rid("user"), _rid("pwd")
    err = f'<div class="msg err" role="alert">{_e(error)}</div>' if error else ""
    return _page(request, "Log On", f"""<div class="panel" style="max-width:420px;margin:40px auto">
<h1>Log On</h1>{err}<form method="post" action="{_base(request)}/ui/login">
<label for="{u}">User</label><input id="{u}" name="j_username" autocomplete="username">
<label for="{p}">Password</label><input id="{p}" name="j_password" type="password" autocomplete="current-password">
<div class="row"><button type="submit" id="{_rid('logon')}">Log On</button></div></form></div>""")


@router.post("/ui/login", include_in_schema=False)
async def ui_login_post(request: Request):
    form = await request.form()
    user, pwd = str(form.get("j_username", "")).strip(), str(form.get("j_password", ""))
    await asyncio.sleep(mb.latency(0.3, 0.9))
    if not user or not pwd:
        return ui_login(request, error="Enter user and password")
    sid = secrets.token_urlsafe(16)
    _sessions[sid] = user
    r = RedirectResponse(f"{_base(request)}/ui/home", status_code=303)
    r.set_cookie("brim_sid", sid, httponly=True, samesite="lax", path="/")
    return r


@router.get("/ui/logout", include_in_schema=False)
def ui_logout(request: Request):
    _sessions.pop(request.cookies.get("brim_sid", ""), None)
    return _need_login(request)


@router.get("/ui/home", include_in_schema=False)
def ui_home(request: Request):
    user = _user(request)
    if not user:
        return _need_login(request)
    b, q, go, res = _base(request), _rid("search"), _rid("go"), _rid("results")
    return _page(request, "Find Account", f"""<div class="panel"><h1>Find Business Partner / Contract Account</h1>
<form id="{_rid('f')}" onsubmit="return doSearch(event)"><label for="{q}">BAN / contract account, business partner or name</label>
<input id="{q}" name="q" data-field="search"><div class="row"><button type="submit" id="{go}">Search</button></div></form></div>
<div class="panel"><h2>Results</h2><div id="{res}" data-role="results"><p class="busy">Enter a search term.</p></div></div>
<script>
function doSearch(ev){{ev.preventDefault();var box=document.getElementById('{res}');box.innerHTML='<p class="busy">Searching…</p>';
fetch('{b}/ui/api/search?q='+encodeURIComponent(document.getElementById('{q}').value)).then(r=>r.json()).then(function(d){{
 if(!d.length){{box.innerHTML='<p>No accounts found.</p>';return;}}
 var h='<table><thead><tr><th>Contract account</th><th>Business partner</th><th>Name</th><th>Wire center</th><th>City</th><th></th></tr></thead><tbody>';
 d.forEach(function(a){{h+='<tr data-ca="'+a.contract_account+'"><td>'+a.contract_account+'</td><td>'+a.business_partner+'</td><td>'+a.customer_name+'</td><td>'+(a.clli||'')+'</td><td>'+(a.city||'')+'</td><td><a class="btn" data-role="open-account" id="open-'+Math.random().toString(36).slice(2,8)+'" href="{b}/ui/account/'+a.contract_account+'">Open</a></td></tr>';}});
 box.innerHTML=h+'</tbody></table>';}});return false;}}
</script>""", user)


@router.get("/ui/api/search", include_in_schema=False)
def ui_api_search(request: Request, q: str = ""):
    if not _user(request):
        return JSONResponse([], status_code=401)
    time.sleep(mb.latency(0.4, 1.3))
    return [{k: a.get(k) for k in ("contract_account", "business_partner", "customer_name", "clli", "city")}
            for a in mb.search_accounts(q)]


@router.get("/ui/api/reasons", include_in_schema=False)
def ui_api_reasons(request: Request, type: str = ""):
    if not _user(request):
        return JSONResponse([], status_code=401)
    time.sleep(mb.latency(0.2, 0.8))
    return [{"code": k, "text": v} for k, v in mb.REASONS.get(type, {}).items()]


@router.get("/ui/account/{ca}", include_in_schema=False)
def ui_account(request: Request, ca: str):
    user = _user(request)
    if not user:
        return _need_login(request)
    time.sleep(mb.latency(0.3, 1.0))
    a = mb.get_account(ca)
    if not a:
        return _page(request, "Account", f'<div class="msg err" role="alert">Contract account {_e(ca)} not found</div>', user)
    b = _base(request)
    rows = "".join(
        f'<tr data-ext-ref="{_e(d.get("CaseExternalReference"))}"><td><a href="{b}/ui/dispute/{d["CaseID"]}">{d["CaseID"]}</a></td>'
        f'<td>{_e(d.get("CaseExternalReference"))}</td><td>{_e(d["CaseType"])}</td><td>{_e(d["CaseReason"])}</td>'
        f'<td>{_e(d.get("DisputedAmount"))} {_e(d.get("DisputedAmountCurrency"))}</td><td>{_e(d["CaseStatus"])}</td></tr>'
        for d in mb.for_account(a["contract_account"])) or '<tr><td colspan="6">No dispute cases</td></tr>'
    btn = _rid("createDispute")
    return _page(request, f"Account {a['contract_account']}", f"""<div class="panel" data-lazy><h1>{_e(a['customer_name'])}</h1>
<table class="kv"><tr><td>Contract account</td><td data-field="contract-account">{a['contract_account']}</td></tr>
<tr><td>Business partner</td><td>{a['business_partner']}</td></tr><tr><td>BAN</td><td data-field="ban">{a['contract_account']}</td></tr><tr><td>Wire center (CLLI)</td><td>{_e(a.get('clli') or '')}</td></tr>
<tr><td>Location</td><td>{_e(a.get('city'))} {_e(a.get('state'))}</td></tr></table>
<div class="row"><form method="post" action="{b}/ui/account/{a['contract_account']}/dispute/new">
<button type="submit" id="{btn}" disabled>Create Dispute Case</button></form></div></div>
<div class="panel"><h2>Dispute cases</h2><table data-role="dispute-list"><thead><tr><th>Case ID</th><th>External reference</th><th>Type</th>
<th>Reason</th><th>Amount</th><th>Status</th></tr></thead><tbody>{rows}</tbody></table></div>
<script>setTimeout(function(){{document.getElementById('{btn}').disabled=false;}},700+Math.random()*900);</script>""", user)


@router.post("/ui/account/{ca}/dispute/new", include_in_schema=False)
def ui_new(request: Request, ca: str):
    user = _user(request)
    if not user:
        return _need_login(request)
    a = mb.get_account(ca)
    if not a:
        return _need_login(request)
    did = secrets.token_hex(6)
    _drafts[did] = {"user": user, "ca": a["contract_account"], "bp": a["business_partner"], "name": a["customer_name"],
                    "data": {}, "created": time.time()}
    return RedirectResponse(f"{_base(request)}/ui/draft/{did}/1", status_code=303)


def _steps(n: int) -> str:
    names = ["1 Classification", "2 Amount &amp; reference", "3 Notes &amp; save"]
    return '<div class="steps">' + "".join(
        f'<span class="{"on" if i + 1 == n else ""}">{s}</span>' for i, s in enumerate(names)) + "</div>"


def _draft(request: Request, did: str):
    user = _user(request)
    d = _drafts.get(did)
    if not user or not d or d["user"] != user:
        return None, None
    return user, d


@router.get("/ui/draft/{did}/{step}", include_in_schema=False)
def ui_step(request: Request, did: str, step: int, error: str = ""):
    user, d = _draft(request, did)
    if not d:
        return _need_login(request)
    b, data = _base(request), d["data"]
    err = f'<div class="msg err" role="alert">{_e(error)}</div>' if error else ""
    head = f'<h1>Create Dispute Case &mdash; {_e(d["name"])} ({d["ca"]})</h1>{_steps(step)}{err}'
    act = f"{b}/ui/draft/{did}/{step}"
    if step == 1:
        t, r = _rid("caseType"), _rid("reason")
        opts = "".join(f'<option value="{k}" {"selected" if data.get("CaseType") == k else ""}>{k} - {v}</option>'
                       for k, v in mb.CASE_TYPES.items())
        body = f"""<form method="post" action="{act}" data-lazy><label for="{t}">Case type</label>
<select id="{t}" name="CaseType" data-field="case-type"><option value="">-- select --</option>{opts}</select>
<label for="{r}">Reason code</label><select id="{r}" name="CaseReason" data-field="reason" disabled><option value="">-- select case type first --</option></select>
<div class="row"><button type="submit" id="{_rid('next')}">Next</button></div></form>
<script>(function(){{var t=document.getElementById('{t}'),r=document.getElementById('{r}');
function load(){{r.disabled=true;r.innerHTML='<option value="">Loading…</option>';if(!t.value)return;
fetch('{b}/ui/api/reasons?type='+t.value).then(x=>x.json()).then(function(l){{r.innerHTML='<option value="">-- select --</option>'+
l.map(function(o){{return '<option value="'+o.code+'">'+o.code+' - '+o.text+'</option>';}}).join('');r.disabled=false;}});}}
t.addEventListener('change',load);if(t.value)load();}})();</script>"""
    elif step == 2:
        am, cu, inv, ext = _rid("amount"), _rid("currency"), _rid("invoice"), _rid("extRef")
        body = f"""<form method="post" action="{act}" data-lazy><label for="{am}">Disputed amount</label>
<input id="{am}" name="DisputedAmount" data-field="amount" inputmode="decimal" value="{_e(data.get('DisputedAmount'))}">
<label for="{cu}">Currency</label><select id="{cu}" name="DisputedAmountCurrency" data-field="currency"><option>USD</option><option>CAD</option></select>
<label for="{inv}">Invoice reference</label><input id="{inv}" name="InvoiceReference" data-field="invoice" value="{_e(data.get('InvoiceReference'))}" placeholder="BAN-YYMM, e.g. 3100000008-2609">
<label for="{ext}">External reference</label><input id="{ext}" name="CaseExternalReference" data-field="external-ref" value="{_e(data.get('CaseExternalReference'))}">
<div class="row"><a class="btn sec" href="{b}/ui/draft/{did}/1">Back</a><button type="submit" id="{_rid('next')}">Next</button></div></form>"""
    else:
        nt = _rid("notes")
        rows = "".join(f"<tr><td>{k}</td><td>{_e(v)}</td></tr>" for k, v in data.items())
        body = f"""<form method="post" action="{act}" data-lazy><table class="kv" data-role="review">{rows}</table>
<label for="{nt}">Notes</label><textarea id="{nt}" name="Notes" data-field="notes">{_e(data.get('Notes'))}</textarea>
<div class="row"><a class="btn sec" href="{b}/ui/draft/{did}/2">Back</a><button type="submit" id="{_rid('save')}">Save Dispute Case</button></div></form>"""
    return _page(request, "Create Dispute Case", f'<div class="panel">{head}{body}</div>', user)


@router.post("/ui/draft/{did}/{step}", include_in_schema=False)
async def ui_step_post(request: Request, did: str, step: int):
    user, d = _draft(request, did)
    if not d:
        return _need_login(request)
    form = {k: str(v).strip() for k, v in (await request.form()).items()}
    b = _base(request)
    data = d["data"]
    await asyncio.sleep(mb.latency(0.2, 0.8))
    if step == 1:
        if form.get("CaseType") not in mb.CASE_TYPES or form.get("CaseReason") not in mb.REASONS.get(form.get("CaseType"), {}):
            return ui_step(request, did, 1, error="Select a case type and a valid reason code")
        data.update(CaseType=form["CaseType"], CaseReason=form["CaseReason"])
        return RedirectResponse(f"{b}/ui/draft/{did}/2", status_code=303)
    if step == 2:
        data.update({k: form.get(k, "") for k in ("DisputedAmount", "DisputedAmountCurrency", "InvoiceReference",
                                                  "CaseExternalReference")})
        try:
            if data["DisputedAmount"]:
                float(data["DisputedAmount"].replace(",", "").lstrip("$"))
        except ValueError:
            return ui_step(request, did, 2, error="Disputed amount is not a valid number")
        return RedirectResponse(f"{b}/ui/draft/{did}/3", status_code=303)
    data["Notes"] = form.get("Notes", "")
    await asyncio.sleep(mb.latency(0.5, 1.4))
    if secrets.randbelow(10000) < mb.fail_rate() * 10000:
        return ui_step(request, did, 3, error="System is busy: dispute case could not be saved (lock FDM_CASE). Try again.")
    rec = {"CaseType": data["CaseType"], "CaseReason": data["CaseReason"], "ContractAccount": d["ca"],
           "BusinessPartner": d["bp"], "CaseTitle": f"{mb.REASONS[data['CaseType']][data['CaseReason']]}: {d['name']}"[:80],
           "DisputedAmount": data.get("DisputedAmount", "").replace(",", "").lstrip("$") or None,
           "DisputedAmountCurrency": data.get("DisputedAmountCurrency") or "USD",
           "InvoiceReference": data.get("InvoiceReference") or None,
           "CaseExternalReference": data.get("CaseExternalReference") or None,
           "_Note": [{"NoteType": "0001", "NoteText": data["Notes"]}] if data.get("Notes") else []}
    try:
        rec_out = mb.create(rec, user, "WEBUI")
    except mb.MockError as e:
        return ui_step(request, did, 3, error=str(e))
    _drafts.pop(did, None)
    return RedirectResponse(f"{b}/ui/dispute/{rec_out['CaseID']}?created=1", status_code=303)


@router.get("/ui/dispute/{cid}", include_in_schema=False)
def ui_dispute(request: Request, cid: str, created: int = 0):
    user = _user(request)
    if not user:
        return _need_login(request)
    d = mb.load()["disputes"].get(cid)
    if not d:
        return _page(request, "Dispute", f'<div class="msg err" role="alert">Dispute case {_e(cid)} not found</div>', user)
    msg = (f'<div class="msg ok" role="status">Dispute case <b data-role="dispute-id">{cid}</b> was created.</div>'
           if created else "")
    rows = "".join(f"<tr><td>{k}</td><td>{_e(json.dumps(v) if isinstance(v, (list, dict)) else v)}</td></tr>"
                   for k, v in d.items())
    return _page(request, f"Dispute {cid}", f"""{msg}<div class="panel"><h1>Dispute case {cid}</h1>
<table class="kv">{rows}</table><div class="row"><a class="btn sec" href="{_base(request)}/ui/account/{d['ContractAccount']}">Back to account</a>
</div></div>""", user)
