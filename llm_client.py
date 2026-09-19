"""
llm_client.py
=============
Wrapper around IBM Watsonx.ai's /ml/v1/text/chat endpoint, now targeting the
open-weight `openai/gpt-oss-120b` model instead of Watsonx-hosted Llama 3.3,
using forced TOOL CALLING instead of "ask for JSON, regex it out of the
reply". This replaces the old call_llm_json() internals — the public
function name/signature is unchanged, so intent_parser.py needs NO edits.

WHY TOOL CALLING INSTEAD OF THE OLD PROMPT-FOR-JSON APPROACH
--------------------------------------------------------------
The previous version asked the model to "return only JSON" and then found
the {...} substring in free text. That's fragile (code fences, stray prose,
truncation). Tool calling makes the model's output conform to a JSON Schema
server-side, and the arguments come back as a clean JSON string — no
string-scraping needed. This mirrors the tool_choice-forced pattern you
pasted; the transport is just watsonx/OpenAI-style (`tools` / `tool_calls`)
rather than Anthropic's (`tools` / `content` blocks), because gpt-oss-120b is
served through watsonx.ai's chat endpoint, not the Anthropic API.

IMPORTANT — verify against your account
-----------------------------------------
IBM's public tool-calling docs (as of writing) list a specific set of models
certified for tool calling (llama-3.3-70b-instruct, mistral-large, granite,
etc.). gpt-oss-120b being chat-capable doesn't guarantee it's on that list
for YOUR account/region yet. Test the `tool_choice` call below against your
project first — if the model rejects forced tool_choice, fall back to
"tool_choice_option": "auto" (or "required") and check `finish_reason`.

Auth: set these env vars (do NOT hardcode secrets in this file):
    WX_API_KEY
    WX_PROJECT_ID
    WX_URL              e.g. https://us-south.ml.cloud.ibm.com
"""

import os
import json
import time
import requests
from config import settings

WATSONX_URL = settings.WX_URL
WATSONX_API_KEY = settings.WX_API_KEY
WATSONX_PROJECT_ID = settings.WX_PROJECT_ID
MODEL_ID = settings.WX_MODEL_ID
IAM_TOKEN_URL = settings.WX_IAM_TOKEN_URL
API_VERSION = "2024-05-31"

_token_cache = {"token": None, "expires_at": 0}

def _get_iam_token() -> str:
    if _token_cache["token"] and time.time() < _token_cache["expires_at"] - 60:
        return _token_cache["token"]
    resp = requests.post(
        settings.WX_IAM_TOKEN_URL,
        data={
            "grant_type": "urn:ibm:params:oauth:grant-type:apikey",
            "apikey": WATSONX_API_KEY,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    _token_cache["token"] = data["access_token"]
    _token_cache["expires_at"] = time.time() + data.get("expires_in", 3600)
    return _token_cache["token"]


def call_llm_json(system_prompt: str, user_prompt: str, tool_schema: dict, max_tokens: int = 500) -> dict:
    """Forces a tool call to extract_intent and returns its arguments as a dict.

    Public signature is unchanged from the previous version - this is a
    drop-in replacement, intent_parser.py doesn't need to change at all.
    """
    token = _get_iam_token()
    url = f"{WATSONX_URL}/ml/v1/text/chat?version={API_VERSION}"
    payload = {
        "model_id": MODEL_ID,
        "project_id": WATSONX_PROJECT_ID,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "tools": [tool_schema],
        "tool_choice": {"type": "function", "function": {"name": "extract_intent"}},
        "max_tokens": max_tokens,
        "temperature": 0,
    }
    resp = requests.post(
        url,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=payload,
        timeout=30,
    )
    # resp.raise_for_status()
    if not resp.ok:
        print("Status:", resp.status_code)
        print("Response:", resp.text)
        resp.raise_for_status()
    data = resp.json()

    message = data["choices"][0]["message"]
    tool_calls = message.get("tool_calls") or []
    for call in tool_calls:
        if call.get("function", {}).get("name") == "extract_intent":
            arguments = call["function"]["arguments"]
            # arguments is a JSON-encoded string per the watsonx/OpenAI shape
            return json.loads(arguments) if isinstance(arguments, str) else arguments

    raise RuntimeError(
        f"gpt-oss-120b did not return a tool_calls block for extract_intent: {message!r}"
    )


def call_llm(system_prompt: str, user_prompt: str, max_tokens: int = 500) -> str:
    """Plain (non-tool-calling) chat call, kept for anything that just wants
    free text back rather than structured extraction."""
    token = _get_iam_token()
    url = f"{WATSONX_URL}/ml/v1/text/chat?version={API_VERSION}"
    payload = {
        "model_id": MODEL_ID,
        "project_id": WATSONX_PROJECT_ID,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": max_tokens,
        "temperature": 0,
    }
    resp = requests.post(
        url,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=payload,
        timeout=30,
    )
    # resp.raise_for_status()
    if not resp.ok:
        print("Status:", resp.status_code)
        print("Response:", resp.text)
        resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]