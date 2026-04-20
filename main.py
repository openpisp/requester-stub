"""
PSP - Requester Stub
Simulates a requester platform (e.g. a payment terminal or web checkout).
Creates payment requests via the PISP and polls for completion.

Serves two interfaces:
- /ui/*         HTMX terminal web UI (desktop, point-of-sale demo)
- /terminal/*   REST API (used by automated tests and the convenience endpoints)
- /admin/*      Observability
"""

import asyncio
import base64
import json
import logging
import os
from typing import Optional

import httpx
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("requester-stub")

app = FastAPI(
    title="PSP - Requester Stub",
    version="0.1.0",
    description=(
        "Simulates a requester platform (payment terminal, web checkout, etc.). "
        "Creates payment requests via the PISP and polls for settlement. "
        "Provides both a browsable HTMX terminal UI (/ui/) and a REST API (/terminal/) "
        "for test automation."
    ),
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PISP_URL = os.getenv("PISP_URL", "http://pisp:8000")
REQUESTER_URI = os.getenv("REQUESTER_URI", "psp://pisp.openpisp.local/requester/acme-coffee")
REQUESTER_DISPLAY_NAME = os.getenv("REQUESTER_DISPLAY_NAME", "Acme Coffee - Shoreditch")
# Terminal credentials for Protocol A.
# PISP_TERMINAL_CLIENT_ID: UUID of the terminal (used as client_id in /auth/token).
# PISP_TERMINAL_API_KEY: raw secret key (sk-…) used to obtain a JWT.
# When PISP_TERMINAL_CLIENT_ID is set, the stub exchanges the raw key for a
# short-lived JWT at startup and refreshes it every 50 minutes.
# When PISP_TERMINAL_CLIENT_ID is absent (env-var PISP mode), the raw key is
# sent directly as a Bearer token.
PISP_TERMINAL_CLIENT_ID = os.getenv("PISP_TERMINAL_CLIENT_ID", "")
PISP_TERMINAL_API_KEY   = os.getenv("PISP_TERMINAL_API_KEY",   "")

# ---------------------------------------------------------------------------
# JWT state — mutable so the refresh loop can update it in-place
# ---------------------------------------------------------------------------

# Holds the current bearer token (either JWT or raw key).
# All code calls _pisp_headers() which reads from this dict.
_auth: dict[str, str] = {"bearer": PISP_TERMINAL_API_KEY}

# Holds the terminal_uri claim extracted from the current JWT.
# Empty string when running in env-var (raw key) mode.
_terminal_state: dict[str, str] = {"uri": ""}


def _decode_jwt_payload(token: str) -> dict:
    """Decode the payload of a JWT without verifying the signature."""
    try:
        payload_b64 = token.split(".")[1]
        # Restore base64 padding
        payload_b64 += "=" * (-len(payload_b64) % 4)
        return json.loads(base64.b64decode(payload_b64))
    except Exception:
        return {}


async def _refresh_jwt() -> None:
    """Exchange the raw API key for a fresh JWT via POST /auth/token.

    No-op if PISP_TERMINAL_CLIENT_ID is not set (env-var PISP mode).
    Updates _auth["bearer"] in place so all in-flight callers pick it up.
    """
    if not PISP_TERMINAL_CLIENT_ID or not PISP_TERMINAL_API_KEY:
        return
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{PISP_URL}/auth/token",
                data={
                    "grant_type":    "client_credentials",
                    "client_id":     PISP_TERMINAL_CLIENT_ID,
                    "client_secret": PISP_TERMINAL_API_KEY,
                },
                timeout=10.0,
            )
        if resp.status_code == 200:
            token = resp.json().get("access_token")
            if token:
                _auth["bearer"] = token
                payload = _decode_jwt_payload(token)
                _terminal_state["uri"] = payload.get("terminal_uri", "")
                log.info(
                    "JWT refreshed for terminal %s… (terminal_uri=%s)",
                    PISP_TERMINAL_CLIENT_ID[:8],
                    _terminal_state["uri"],
                )
                return
        log.warning("JWT exchange returned HTTP %s — keeping existing token", resp.status_code)
    except Exception as exc:
        log.warning("JWT refresh failed: %s", exc)


async def _jwt_refresh_loop() -> None:
    """Background task: refresh the JWT every 50 minutes (TTL is 1 hour)."""
    while True:
        await asyncio.sleep(50 * 60)
        await _refresh_jwt()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pisp_headers() -> dict:
    """Return Authorization header dict for Protocol A calls, or empty dict if not configured."""
    token = _auth["bearer"]
    if token:
        return {"Authorization": f"Bearer {token}"}
    return {}


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))


def _make_fiat_amount(pence: int, asset_code: str = "GBP") -> dict:
    """Build a fiat Amount dict from a minor-unit integer (e.g. pence)."""
    symbols = {"GBP": "£", "EUR": "€", "USD": "$"}
    symbol = symbols.get(asset_code, asset_code + " ")
    return {
        "value":       pence,
        "asset_kind":  "fiat",
        "asset_code":  asset_code,
        "minor_units": 2,
        "display":     f"{symbol}{pence / 100:.2f}",
    }


def _tpl_ctx(request: Request, **kwargs):
    """Base context injected into every template render."""
    return {"request": request, "merchant_name": REQUESTER_DISPLAY_NAME, **kwargs}

# ---------------------------------------------------------------------------
# In-memory state
# ---------------------------------------------------------------------------

# payment_request_id (str) → full request record (as returned by PISP)
active_requests: dict[str, dict] = {}

# token_id (str) → token record (as returned by PISP)
qr_tokens_cache: dict[str, dict] = {}

# Received webhook events from PISP — newest first, capped at 200
webhook_events: list[dict] = []

# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def _startup():
    """Acquire a JWT (if DB-backed), restore payment history, start refresh loop."""
    # Step 1: exchange raw key for JWT so all subsequent calls use a valid token.
    await _refresh_jwt()

    # Step 2: schedule the background refresh loop (no-op for env-var PISPs).
    if PISP_TERMINAL_CLIENT_ID and PISP_TERMINAL_API_KEY:
        asyncio.create_task(_jwt_refresh_loop())

    # Step 3: restore active_requests from the PISP.
    # Always send requester_uri — used as a fallback when no terminal directory
    # is configured.  When terminal auth is active the Bearer token takes
    # precedence and the PISP returns both terminal-specific payments AND
    # org-level payments (QR-token / payment-link) for the same requester.
    params: dict = {"limit": 200, "requester_uri": REQUESTER_URI}

    for attempt in range(1, 4):
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"{PISP_URL}/requester/requests",
                    params=params,
                    headers=_pisp_headers(),
                    timeout=10.0,
                )
            if resp.status_code == 200:
                payments = resp.json().get("payments", [])
                for p in payments:
                    pr_id = p.get("payment_request_id")
                    if pr_id:
                        active_requests[pr_id] = p
                log.info("Restored %d payment request(s) from PISP", len(payments))
                break
            log.warning("Attempt %d: PISP returned HTTP %s for payment list", attempt, resp.status_code)
        except Exception as exc:
            log.warning("Attempt %d: could not fetch payment history from PISP: %s", attempt, exc)
        if attempt < 3:
            await asyncio.sleep(2 * attempt)
    else:
        log.warning("Could not restore payment history from PISP — starting with empty cache")

    # Step 4: restore qr_tokens_cache from the PISP so static tokens survive stub restarts.
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{PISP_URL}/requester/qr-tokens",
                params={"requester_uri": REQUESTER_URI},
                headers=_pisp_headers(),
                timeout=10.0,
            )
        if resp.status_code == 200:
            tokens = resp.json().get("tokens", [])
            for t in tokens:
                qr_tokens_cache[t["token_id"]] = t
            log.info("Restored %d QR token(s) from PISP", len(tokens))
        else:
            log.warning("Could not restore QR tokens from PISP (HTTP %s)", resp.status_code)
    except Exception as exc:
        log.warning("Could not restore QR tokens from PISP: %s", exc)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/health", tags=["Admin"])
def health():
    return {
        "status": "ok",
        "component": "requester-stub",
        "requester_uri": REQUESTER_URI,
        "pisp_url": PISP_URL,
    }


# ---------------------------------------------------------------------------
# UI routes (HTMX / browser)
# ---------------------------------------------------------------------------

@app.get("/ui/", response_class=HTMLResponse, tags=["UI"], include_in_schema=False)
async def ui_home(request: Request, payment_request_id: Optional[str] = None):
    """Terminal home — new payment form. Optionally redisplays a live payment."""
    if payment_request_id and payment_request_id in active_requests:
        pr = active_requests[payment_request_id]
        return templates.TemplateResponse(
            request,
            "new_payment.html",
            _tpl_ctx(request, terminal_pr=pr, error=None),
        )
    return templates.TemplateResponse(request, "new_payment.html", _tpl_ctx(request, error=None))


@app.post("/ui/payment", response_class=HTMLResponse, tags=["UI"], include_in_schema=False)
async def ui_create_payment(
    request: Request,
    reference: str = Form(...),
    amount_pounds: float = Form(...),
    expires_in_seconds: int = Form(300),
    description: Optional[str] = Form(None),
):
    """
    HTMX: create a payment request and render the terminal display partial
    (QR + live status panel) into #terminal-area.
    """
    amount_pence = round(amount_pounds * 100)
    payload = {
        "mode": "once",
        "requester_uri": REQUESTER_URI,
        "display_name": REQUESTER_DISPLAY_NAME,
        "amount": _make_fiat_amount(amount_pence),
        "reference": reference,
        "description": description or None,
        "expires_in_seconds": expires_in_seconds,
    }

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{PISP_URL}/requester/requests",
            json=payload,
            headers=_pisp_headers(),
            timeout=10.0,
        )

    if resp.status_code != 200:
        return templates.TemplateResponse(
            request,
            "error.html",
            _tpl_ctx(
                request,
                title="PISP Error",
                detail=f"Could not create payment request: {resp.text}",
                retry_url="/ui/",
            ),
            status_code=502,
        )

    pr = resp.json()
    active_requests[pr["payment_request_id"]] = pr
    log.info("UI created payment request %s (%s)", pr["payment_request_id"], pr["amount"]["display"])

    # qr_payload comes from the create response only — preserve it separately
    # so poll updates (which don't include it) don't overwrite it.
    qr_payload = pr.get("qr_payload", "")

    return templates.TemplateResponse(request, "terminal.html", _tpl_ctx(request, pr=pr, qr_payload=qr_payload))


@app.get(
    "/ui/payment/{payment_request_id}/status",
    response_class=HTMLResponse,
    tags=["UI"],
    include_in_schema=False,
)
async def ui_payment_status(request: Request, payment_request_id: str):
    """
    HTMX polling endpoint: fetch current payment status from PISP and render
    the status_panel partial. Called every 2s by the terminal display.
    """
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{PISP_URL}/requester/requests/{payment_request_id}",
            headers=_pisp_headers(),
            timeout=10.0,
        )

    if resp.status_code == 404:
        return templates.TemplateResponse(
            request,
            "error.html",
            _tpl_ctx(request, title="Not Found", detail="Payment request not found.", retry_url="/ui/"),
            status_code=404,
        )
    if resp.status_code != 200:
        return templates.TemplateResponse(
            request,
            "error.html",
            _tpl_ctx(request, title="Error", detail=f"PISP returned {resp.status_code}.", retry_url="/ui/"),
            status_code=502,
        )

    pr = resp.json()
    # Merge poll result into cache — preserve qr_payload from the original
    # create response since the poll endpoint does not return it.
    existing = active_requests.get(payment_request_id, {})
    pr.setdefault("qr_payload", existing.get("qr_payload", ""))
    active_requests[payment_request_id] = pr
    status = pr.get("status", "PENDING")

    return templates.TemplateResponse(
        request,
        "status_panel.html",
        _tpl_ctx(request, pr=pr, status=status),
    )


@app.delete(
    "/ui/payment/{payment_request_id}",
    response_class=HTMLResponse,
    tags=["UI"],
    include_in_schema=False,
)
async def ui_cancel_payment(request: Request, payment_request_id: str):
    """HTMX: cancel a payment and render the status panel with CANCELLED state."""
    async with httpx.AsyncClient() as client:
        resp = await client.delete(
            f"{PISP_URL}/requester/requests/{payment_request_id}",
            headers=_pisp_headers(),
            timeout=10.0,
        )

    if resp.status_code == 404:
        return templates.TemplateResponse(
            request,
            "error.html",
            _tpl_ctx(request, title="Not Found", detail="Payment request not found.", retry_url="/ui/"),
            status_code=404,
        )
    if resp.status_code == 409:
        detail = resp.json().get("detail", "Cannot cancel a payment that is already in a terminal state.")
        return templates.TemplateResponse(
            request,
            "error.html",
            _tpl_ctx(request, title="Cannot Cancel", detail=detail, retry_url="/ui/"),
            status_code=409,
        )

    pr = resp.json()
    active_requests[payment_request_id] = pr
    log.info("UI cancelled payment request %s", payment_request_id)

    return templates.TemplateResponse(
        request,
        "status_panel.html",
        _tpl_ctx(request, pr=pr, status=pr.get("status", "CANCELLED")),
    )


@app.get("/ui/history", response_class=HTMLResponse, tags=["UI"], include_in_schema=False)
async def ui_history(request: Request):
    """History of all payment requests created in this session."""
    return templates.TemplateResponse(
        request,
        "history.html",
        _tpl_ctx(request, requests=list(active_requests.values())),
    )


@app.get(
    "/ui/payment/{payment_request_id}",
    response_class=HTMLResponse,
    tags=["UI"],
    include_in_schema=False,
)
async def ui_payment_detail(request: Request, payment_request_id: str):
    """Detail view for a specific payment — fetches fresh status from PISP."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{PISP_URL}/requester/requests/{payment_request_id}",
            headers=_pisp_headers(),
            timeout=10.0,
        )

    if resp.status_code == 404:
        return templates.TemplateResponse(
            request,
            "error.html",
            _tpl_ctx(request, title="Not Found", detail="Payment request not found.", retry_url="/ui/history"),
            status_code=404,
        )
    if resp.status_code != 200:
        return templates.TemplateResponse(
            request,
            "error.html",
            _tpl_ctx(request, title="Error", detail=f"PISP returned {resp.status_code}.", retry_url="/ui/history"),
            status_code=502,
        )

    pr = resp.json()
    # Preserve qr_payload from the original create response (poll doesn't return it).
    existing = active_requests.get(payment_request_id, {})
    pr.setdefault("qr_payload", existing.get("qr_payload", ""))
    active_requests[payment_request_id] = pr
    return templates.TemplateResponse(request, "payment_detail.html", _tpl_ctx(request, pr=pr))


@app.get("/ui/webhooks", response_class=HTMLResponse, tags=["UI"], include_in_schema=False)
async def ui_webhooks(request: Request):
    """Received webhook events from the PISP."""
    return templates.TemplateResponse(
        request,
        "webhooks.html",
        _tpl_ctx(request, events=webhook_events),
    )


@app.get("/ui/qr-tokens", response_class=HTMLResponse, tags=["UI"], include_in_schema=False)
async def ui_qr_tokens(request: Request, msg: Optional[str] = None, error: Optional[str] = None):
    """Static QR token management screen."""
    tokens = list(qr_tokens_cache.values())
    # For armed tokens with an active PR, fetch the current PR status so the
    # UI can show whether the armed payment has been settled (needs re-arming).
    async with httpx.AsyncClient() as client:
        for token in tokens:
            if token.get("mode") == "armed" and token.get("armed_pr_id"):
                try:
                    r = await client.get(
                        f"{PISP_URL}/requester/requests/{token['armed_pr_id']}",
                        headers=_pisp_headers(),
                        timeout=5.0,
                    )
                    if r.status_code == 200:
                        pr = r.json()
                        token["armed_pr_status"] = pr.get("status", "")
                        token["armed_pr_amount"] = pr.get("amount")
                        token["armed_pr_reference"] = pr.get("reference", "")
                        token["armed_pr_description"] = pr.get("description", "")
                    else:
                        token["armed_pr_status"] = ""
                except Exception:
                    token["armed_pr_status"] = ""
    return templates.TemplateResponse(
        request,
        "qr_tokens.html",
        _tpl_ctx(request, tokens=tokens, msg=msg, error=error),
    )


@app.post("/ui/qr-tokens", response_class=HTMLResponse, tags=["UI"], include_in_schema=False)
async def ui_register_qr_token(
    request: Request,
    display_name: str = Form(...),
    mode: str = Form("armed"),
    amount_pounds: Optional[float] = Form(None),
    reference: Optional[str] = Form(None),
    description: Optional[str] = Form(None),
    expires_in_seconds: int = Form(300),
):
    """Register a new static QR token."""
    # owner_uri must be the org-level requester URI — the PISP validates that
    # the terminal's requester_uri (from the JWT) matches it.  Terminal-specific
    # attribution happens via set_terminal on the PISP side when the payer scans.
    payload = {
        "requester_uri": REQUESTER_URI,
        "terminal_uri": _terminal_state.get("uri", ""),
        "display_name": display_name,
        "mode": mode,
    }
    if mode == "template":
        if amount_pounds is not None:
            payload["amount"] = _make_fiat_amount(round(amount_pounds * 100))
        if reference:
            payload["reference"] = reference
        if description:
            payload["description"] = description
        payload["expires_in_seconds"] = expires_in_seconds

    async with httpx.AsyncClient() as client:
        resp = await client.post(f"{PISP_URL}/requester/requests", json=payload, headers=_pisp_headers(), timeout=10.0)

    if resp.status_code != 200:
        return templates.TemplateResponse(
            request,
            "qr_tokens.html",
            _tpl_ctx(
                request,
                tokens=list(qr_tokens_cache.values()),
                msg=None,
                error=f"PISP error: {resp.text}",
            ),
            status_code=502,
        )

    token = resp.json()
    # Normalise template tokens so the Jinja2 template can find amount/reference
    # under t.template (same shape as the GET /requester/qr-tokens restore path).
    if token.get("mode") == "template":
        token.setdefault("template", {
            "amount": token.get("amount"),
            "reference": token.get("reference"),
            "description": token.get("description"),
            "expires_in_seconds": token.get("expires_in_seconds"),
        })
    qr_tokens_cache[token["token_id"]] = token
    return templates.TemplateResponse(
        request,
        "qr_tokens.html",
        _tpl_ctx(
            request,
            tokens=list(qr_tokens_cache.values()),
            msg=f"Token '{display_name}' registered.",
            error=None,
        ),
    )


@app.post(
    "/ui/qr-tokens/{token_id}/rearm",
    response_class=HTMLResponse,
    tags=["UI"],
    include_in_schema=False,
)
async def ui_rearm_qr_token(
    request: Request,
    token_id: str,
    amount_pounds: float = Form(...),
    reference: str = Form(...),
    description: Optional[str] = Form(None),
    expires_in_seconds: int = Form(300),
):
    """Re-arm an armed QR token with a fresh payment amount."""
    payload = {
        "amount": _make_fiat_amount(round(amount_pounds * 100)),
        "reference": reference,
        "description": description or None,
        "expires_in_seconds": expires_in_seconds,
    }
    async with httpx.AsyncClient() as client:
        resp = await client.put(
            f"{PISP_URL}/requester/requests/{token_id}/arm", json=payload, headers=_pisp_headers(), timeout=10.0
        )

    if resp.status_code != 200:
        detail = resp.json().get("detail", resp.text) if resp.content else resp.text
        return templates.TemplateResponse(
            request,
            "qr_tokens.html",
            _tpl_ctx(
                request,
                tokens=list(qr_tokens_cache.values()),
                msg=None,
                error=f"Re-arm failed: {detail}",
            ),
            status_code=resp.status_code,
        )

    pr = resp.json()
    # Update the cached token's armed_pr_id and add to active_requests so the
    # history page shows the payment immediately (without waiting for a restart).
    pr_id = pr.get("payment_request_id")
    if token_id in qr_tokens_cache:
        t = qr_tokens_cache[token_id]
        t["armed_pr_id"] = pr_id
        t["armed_pr_status"] = pr.get("status", "PENDING")
        t["armed_pr_amount"] = pr.get("amount")
        t["armed_pr_reference"] = pr.get("reference", "")
        t["armed_pr_description"] = pr.get("description", "")
    if pr_id:
        active_requests[pr_id] = pr
    log.info("Re-armed QR token %s → pr %s", token_id, pr_id)
    return templates.TemplateResponse(
        request,
        "qr_tokens.html",
        _tpl_ctx(
            request,
            tokens=list(qr_tokens_cache.values()),
            msg=f"Token re-armed: {pr['amount']['display']} / {pr['reference']}",
            error=None,
        ),
    )


@app.delete(
    "/ui/qr-tokens/{token_id}",
    response_class=HTMLResponse,
    tags=["UI"],
    include_in_schema=False,
)
async def ui_revoke_qr_token(request: Request, token_id: str):
    """Revoke (disable) a QR token."""
    async with httpx.AsyncClient() as client:
        resp = await client.delete(
            f"{PISP_URL}/requester/requests/{token_id}", headers=_pisp_headers(), timeout=10.0
        )

    if resp.status_code not in (200, 404):
        return templates.TemplateResponse(
            request,
            "qr_tokens.html",
            _tpl_ctx(
                request,
                tokens=list(qr_tokens_cache.values()),
                msg=None,
                error=f"Revoke failed: {resp.text}",
            ),
            status_code=502,
        )

    if token_id in qr_tokens_cache:
        qr_tokens_cache[token_id]["status"] = "revoked"
    return templates.TemplateResponse(
        request,
        "qr_tokens.html",
        _tpl_ctx(
            request,
            tokens=list(qr_tokens_cache.values()),
            msg="Token revoked.",
            error=None,
        ),
    )


# ---------------------------------------------------------------------------
# Terminal REST API (used by tests and CI)
# ---------------------------------------------------------------------------

class NewPaymentBody(BaseModel):
    amount_pence: int
    reference: str
    description: Optional[str] = None
    expires_in_seconds: int = 300


@app.post(
    "/terminal/payment",
    tags=["Terminal"],
    summary="Create a new payment request (simulates operator entering amount on terminal)",
)
async def create_payment(body: NewPaymentBody):
    """
    Simulates a terminal operator entering an amount. Calls the PISP to create
    a payment request and returns the QR payload and payment_request_id.
    The terminal would display the QR code for the payer to scan.
    """
    payload = {
        "mode": "once",
        "requester_uri": REQUESTER_URI,
        "display_name": REQUESTER_DISPLAY_NAME,
        "amount": _make_fiat_amount(body.amount_pence),
        "reference": body.reference,
        "description": body.description,
        "expires_in_seconds": body.expires_in_seconds,
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{PISP_URL}/requester/requests",
            json=payload,
            headers=_pisp_headers(),
            timeout=10.0,
        )

    if resp.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"PISP rejected payment request: {resp.text}"
        )

    data = resp.json()
    active_requests[data["payment_request_id"]] = data
    log.info(
        "Payment request created: %s (%s)",
        data["payment_request_id"],
        data["amount"]["display"],
    )
    return data


@app.get(
    "/terminal/payment/{payment_request_id}",
    tags=["Terminal"],
    summary="Poll payment status (simulates terminal waiting for confirmation)",
)
async def poll_payment(payment_request_id: str):
    """
    Polls the PISP for the current status of a payment request.
    The terminal calls this repeatedly until status is SETTLED, FAILED,
    EXPIRED or CANCELLED.
    """
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{PISP_URL}/requester/requests/{payment_request_id}",
            headers=_pisp_headers(),
            timeout=10.0,
        )

    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail="Payment request not found")

    data = resp.json()
    log.info("Polled %s → %s", payment_request_id, data.get("status"))
    return data


@app.delete(
    "/terminal/payment/{payment_request_id}",
    tags=["Terminal"],
    summary="Cancel a payment request (simulates operator cancelling on terminal)",
)
async def cancel_payment(payment_request_id: str):
    """
    Cancels a pending payment request. Simulates the terminal operator
    pressing cancel before the payer has scanned/paid.
    """
    async with httpx.AsyncClient() as client:
        resp = await client.delete(
            f"{PISP_URL}/requester/requests/{payment_request_id}",
            headers=_pisp_headers(),
            timeout=10.0,
        )

    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail="Payment request not found")
    if resp.status_code == 409:
        raise HTTPException(status_code=409, detail=resp.json().get("detail"))

    log.info("Cancelled payment request %s", payment_request_id)
    return resp.json()


@app.post(
    "/terminal/payment/{payment_request_id}/wait",
    tags=["Terminal"],
    summary="Wait for payment to reach a terminal state (convenience endpoint)",
)
async def wait_for_payment(payment_request_id: str, timeout_seconds: int = 60):
    """
    Convenience endpoint that polls the PISP until the payment reaches a
    terminal state (SETTLED, FAILED, EXPIRED, CANCELLED) or times out.
    Simulates the terminal's internal polling loop.
    """
    terminal_states = {"SETTLED", "FAILED", "EXPIRED", "CANCELLED"}
    elapsed = 0
    poll_interval = 1

    while elapsed < timeout_seconds:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{PISP_URL}/requester/requests/{payment_request_id}",
                headers=_pisp_headers(),
                timeout=10.0,
            )

        if resp.status_code != 200:
            raise HTTPException(status_code=502, detail=f"PISP error: {resp.text}")

        data = resp.json()
        status = data.get("status")
        log.info("Waiting on %s → %s (%ds elapsed)", payment_request_id, status, elapsed)

        if status in terminal_states:
            return data

        await asyncio.sleep(poll_interval)
        elapsed += poll_interval

    raise HTTPException(
        status_code=408,
        detail=f"Timed out waiting for payment {payment_request_id} after {timeout_seconds}s"
    )


# ---------------------------------------------------------------------------
# Webhook receiver (PISP → requester platform push notifications)
# ---------------------------------------------------------------------------

@app.post(
    "/webhooks/payment",
    tags=["Webhooks"],
    summary="Receive payment event webhook from PISP",
    status_code=200,
)
async def receive_webhook(request: Request):
    """Accepts signed payment event webhooks from the PISP.

    The PISP POSTs a ``PaymentWebhookEvent`` JSON body here whenever a payment
    reaches a terminal status (SETTLED, FAILED, CANCELLED, EXPIRED).
    The ``X-PSP-Signature`` header carries the JWS signature for verification.
    """
    from datetime import datetime, timezone as _tz
    body = await request.json()
    webhook_events.insert(0, {
        **body,
        "_received_at": datetime.now(_tz.utc).isoformat(),
        "_signature": request.headers.get("x-psp-signature", ""),
    })
    if len(webhook_events) > 200:
        webhook_events.pop()
    log.info(
        "Webhook received: %s for payment %s",
        body.get("event_type"), body.get("payment_request_id"),
    )
    # Ensure the payment appears in the History tab even when it was created
    # by the PISP internally (e.g. template QR token scan) rather than via a
    # POST /requester/requests call from this stub.
    pr_id = body.get("payment_request_id")
    if pr_id:
        # Derive status from event_type so the history template can display it.
        _event_status = {
            "payment.settled":   "SETTLED",
            "payment.failed":    "FAILED",
            "payment.cancelled": "CANCELLED",
            "payment.expired":   "EXPIRED",
        }.get(body.get("event_type", ""))
        enriched = {**body}
        if _event_status:
            enriched["status"] = _event_status
        if pr_id not in active_requests:
            active_requests[pr_id] = enriched
        else:
            active_requests[pr_id] = {**active_requests[pr_id], **enriched}
    return {"received": True}


# ---------------------------------------------------------------------------
# Admin / observability
# ---------------------------------------------------------------------------

@app.get(
    "/admin/requests",
    tags=["Admin"],
    summary="List all payment requests created by this terminal",
)
def list_requests():
    """Shows all payment requests created in this session."""
    return list(active_requests.values())
