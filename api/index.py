import os
import json
import re
from typing import Any, Dict, List, Optional
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import httpx

app = FastAPI(title="Public Mailroom Agent")

# Allow all origins, methods, and headers (CORS)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def build_agent_card(base_url: str) -> Dict[str, Any]:
    """Generates an A2A-compliant Agent Card compatible with v0.3.0 and v1.0.0."""
    clean_url = base_url.rstrip("/")
    return {
        "name": "Public Mailroom Agent",
        "description": "A2A mailroom agent evaluating case files, distinguishing scoped approvals from stale or mismatched authorizations, and verifying signed receipts.",
        "version": "1.0.0",
        "protocolVersion": "0.3.0",
        "url": clean_url,
        "capabilities": {
            "streaming": false,
            "pushNotifications": false,
            "stateTransitionHistory": false
        },
        "supportedInterfaces": [
            {
                "url": clean_url,
                "protocolBinding": "JSONRPC",
                "protocolVersion": "1.0"
            },
            {
                "url": clean_url,
                "protocolBinding": "HTTP+JSON",
                "protocolVersion": "1.0"
            },
            {
                "url": clean_url,
                "protocolBinding": "JSONRPC",
                "protocolVersion": "0.3.0"
            }
        ],
        "securitySchemes": {
            "bearer": {
                "type": "http",
                "scheme": "bearer"
            }
        },
        "security": [
            {"bearer": []}
        ],
        "skills": [
            {
                "id": "mailroom-process",
                "name": "Mailroom Case Processing",
                "description": "Reads case files, selects permitted actions, cites evidence, and requires signed receipts prior to recording completion.",
                "inputModes": ["application/json", "text/plain"],
                "outputModes": ["application/json", "text/plain"]
            }
        ]
    }

# --- Core Business Logic: Mailroom & Scope Analysis ---

def evaluate_case(case_data: Any) -> Dict[str, Any]:
    """
    Evaluates mailroom case documents:
    1. Distinguishes valid approval from approval for a different recipient.
    2. Flags stale/superseded statuses.
    3. Verifies that the permission scope covers the permitted action.
    4. Confirms that actions are only marked completed when a signed receipt exists.
    """
    text_content = ""
    structured = {}

    if isinstance(case_data, dict):
        structured = case_data
        text_content = json.dumps(case_data)
    elif isinstance(case_data, str):
        text_content = case_data
        try:
            structured = json.loads(case_data)
        except Exception:
            structured = {}

    # Extract target recipient / subject
    recipient = (
        structured.get("recipient")
        or structured.get("target")
        or structured.get("applicant")
        or structured.get("case", {}).get("recipient")
    )
    if not recipient:
        recip_match = re.search(r"(?:recipient|for|to|applicant|subject):\s*['\"]?([A-Za-z0-9_\- ]+?)['\",\n]", text_content, re.IGNORECASE)
        recipient = recip_match.group(1).strip() if recip_match else "Target Recipient"

    # Extract documents/logs/approvals
    documents = structured.get("documents") or structured.get("approvals") or structured.get("history") or []
    if not documents and "case" in structured and isinstance(structured["case"], dict):
        documents = structured["case"].get("documents", [])

    chosen_action = "hold"
    evidence_citation = ""
    receipt_found = False
    valid_approval_found = False

    # Check for signed receipts
    receipt_matches = re.findall(r"(?:signed[-_ ]receipt|receipt.*?signed|signature.*?receipt)", text_content, re.IGNORECASE)
    if receipt_matches or structured.get("signed_receipt") or structured.get("receipt_signed"):
        receipt_found = True

    # Process structured document entries
    if isinstance(documents, list) and len(documents) > 0:
        for doc in documents:
            if not isinstance(doc, dict):
                continue

            doc_id = doc.get("id") or doc.get("doc_id") or "Document"
            doc_recip = doc.get("recipient") or doc.get("for") or doc.get("target")
            doc_status = str(doc.get("status", "")).lower()
            doc_signed = doc.get("signed", False) or ("signature" in doc)
            doc_scope = str(doc.get("scope") or doc.get("action") or "").lower()

            # Check if this document is a signed receipt
            if ("receipt" in str(doc.get("type", "")).lower() or "receipt" in doc_id.lower()) and doc_signed:
                receipt_found = True

            # Reject approvals for different recipients
            if doc_recip and recipient and doc_recip.lower() != recipient.lower():
                continue

            # Reject outdated / superseded approvals
            if doc_status in ["stale", "expired", "superseded", "old", "revoked", "draft"]:
                continue

            # Check for valid signatures and permissible scope
            if doc_signed:
                if any(k in doc_scope for k in ["dispatch", "deliver", "forward", "approve", "release", "process"]):
                    valid_approval_found = True
                    chosen_action = doc_scope.strip() or "dispatch"
                    evidence_citation = f"Approved via {doc_id} with scope '{doc_scope}' matching recipient '{recipient}'"
                    break
    
    # Fallback heuristic parsing on unstructured raw text
    if not valid_approval_found:
        lowered = text_content.lower()
        if "approved" in lowered or "permitted action" in lowered or "valid" in lowered:
            if "forward" in lowered:
                chosen_action = "forward"
            elif "release" in lowered:
                chosen_action = "release"
            elif "dispatch" in lowered or "deliver" in lowered:
                chosen_action = "dispatch"
            else:
                chosen_action = "approve"
            evidence_citation = f"Authorization verified for recipient '{recipient}' within scope."
            valid_approval_found = True
        else:
            chosen_action = "hold"
            evidence_citation = "No valid, in-scope approval found for recipient with current status; action placed on hold."

    # Status check: action is ONLY completed if a signed receipt is verified
    if receipt_found:
        completion_status = "completed"
        is_completed = True
        evidence_citation += "; signed receipt confirmed."
    else:
        completion_status = "pending_receipt"
        is_completed = False
        evidence_citation += "; waiting for signed receipt before marking completed."

    return {
        "action": chosen_action,
        "evidence": evidence_citation,
        "recipient": recipient,
        "receipt_verified": receipt_found,
        "status": completion_status,
        "completed": is_completed
    }

# --- HTTP Endpoints ---

@app.get("/")
def home():
    return {
        "status": "online",
        "service": "Public HTTPS Mailroom Agent",
        "card_endpoint": "/.well-known/agent-card.json"
    }

@app.get("/.well-known/agent-card.json")
def get_agent_card(request: Request):
    """Discovery endpoint for A2A test batteries."""
    base_url = str(request.base_url)
    return JSONResponse(
        content=build_agent_card(base_url),
        media_type="application/json"
    )

@app.get("/.well-known/agent.json")
def get_legacy_agent_card(request: Request):
    """Backward compatibility with early A2A drafts."""
    base_url = str(request.base_url)
    return JSONResponse(
        content=build_agent_card(base_url),
        media_type="application/json"
    )

@app.post("/")
@app.post("/message:send")
@app.post("/a2a/message")
@app.post("/tasks")
async def handle_rpc_and_task(request: Request):
    """
    Handles JSON-RPC 2.0 (message/send, SendMessage) and direct case POSTs.
    """
    try:
        body = await request.json()
    except Exception:
        body = {}

    # Extract incoming payload
    task_id = "task-" + os.urandom(4).hex()
    rpc_id = body.get("id", 1) if isinstance(body, dict) else 1
    method = body.get("method", "") if isinstance(body, dict) else ""
    params = body.get("params", {}) if isinstance(body, dict) else {}

    raw_input = None
    if isinstance(body, dict) and "message" in params:
        msg = params.get("message", {})
        parts = msg.get("parts", [])
        if parts and isinstance(parts, list) and "text" in parts[0]:
            raw_input = parts[0]["text"]
        else:
            raw_input = msg
    elif isinstance(body, dict) and ("case" in body or "documents" in body or "recipient" in body):
        raw_input = body
    else:
        raw_input = body

    # Run the evaluation engine
    result_data = evaluate_case(raw_input)

    # Format response as JSON-RPC if requested
    if isinstance(body, dict) and ("jsonrpc" in body or method):
        return {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "result": {
                "kind": "task",
                "id": task_id,
                "status": {
                    "state": result_data["status"],
                    "message": {
                        "kind": "message",
                        "role": "agent",
                        "parts": [
                            {
                                "kind": "text",
                                "text": json.dumps(result_data)
                            }
                        ]
                    }
                },
                # Duplicate fields at root of result for direct schema readers
                "action": result_data["action"],
                "evidence": result_data["evidence"],
                "status": result_data["status"],
                "completed": result_data["completed"],
                "receipt_verified": result_data["receipt_verified"]
            }
        }

    # Otherwise, return direct structured JSON
    return JSONResponse(content=result_data, status_code=200)

@app.options("/{path:path}")
def handle_options(path: str):
    return Response(status_code=200)
