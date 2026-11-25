"""The agent's side of the UCP API. The API key and the JWT never leave this module (hard rule 13)."""
import base64
import json
import threading
import uuid
from typing import Any

import httpx
from mcp.server.mcpserver.exceptions import ToolError


class UCPClient:
    def __init__(self, http: httpx.Client, api_key: str):
        self.http, self._api_key = http, api_key
        self._token: str | None = None
        self._lock = threading.Lock()  # tools run on worker threads

    def _login(self, stale: str | None) -> str:
        with self._lock:
            if self._token is None or self._token == stale:
                r = self.http.post("/v1/auth/token", json={"grant_type": "api_key"},
                                   headers={"X-API-Key": self._api_key})
                if r.status_code != 200:
                    raise _error(r)
                self._token = r.json()["token"]
            return self._token

    def agent_id(self) -> str:
        part = (self._token or self._safe(lambda: self._login(None))).split(".")[1]
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))["sub"]  # our own token, just read

    @staticmethod
    def _safe(fn: Any) -> Any:
        try:
            return fn()
        except httpx.HTTPError:
            raise ToolError("UCP did not answer. Check that the UCP API is running, then try again.") from None

    def call(self, method: str, path: str, **kwargs: Any) -> Any:
        def send() -> httpx.Response:
            token = self._token or self._login(None)
            headers = kwargs.pop("headers", {})
            r = self.http.request(method, path, headers={**headers, "Authorization": f"Bearer {token}"}, **kwargs)
            if r.status_code == 401 and _code(r) == "UCP_SESSION_EXPIRED":
                # refused at authentication, before anything ran, so sending it once more is safe
                token = self._login(token)
                r = self.http.request(method, path, headers={**headers, "Authorization": f"Bearer {token}"}, **kwargs)
            return r

        r = self._safe(send)
        if r.status_code >= 300:
            raise _error(r)
        return r.json()

    def intent(self, method: str, params: dict, idempotency_key: str | None) -> Any:
        key = idempotency_key or str(uuid.uuid4())
        try:
            return self.call("POST", "/v1/intent", json={"method": method, "params": params},
                             headers={"Idempotency-Key": key})
        except ToolError as e:
            # the same key makes a retry safe: UCP replays the result instead of acting twice (rules 4 and 5)
            raise ToolError(f"{e}\nTo retry this same request, pass idempotency_key=\"{key}\".") from None


def _code(r: httpx.Response) -> str | None:
    try:
        return r.json()["error"]["code"]
    except (ValueError, KeyError, TypeError):
        return None


def _error(r: httpx.Response) -> ToolError:
    """UCP's whole error, details included: cancel_order's confirm_url arrives there."""
    try:
        return ToolError(json.dumps(r.json()["error"]))
    except (ValueError, KeyError, TypeError):
        return ToolError(f"UCP answered {r.status_code} without an error body")
