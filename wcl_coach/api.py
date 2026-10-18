"""Thin Warcraft Logs v2 GraphQL client.

- OAuth2 client-credentials flow (public API, no user login needed)
- Token cached on disk until shortly before expiry
- Response cache on disk: uploaded reports are immutable, so caching saves
  a lot of the hourly API point budget when you re-run an analysis
- Retry with backoff on 429 / 5xx
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

import requests

SITES = {
    "retail": "https://www.warcraftlogs.com",
    "classic": "https://classic.warcraftlogs.com",
    "fresh": "https://fresh.warcraftlogs.com",
    "sod": "https://sod.warcraftlogs.com",
}

CACHE_DIR = Path(os.environ.get("WCL_COACH_CACHE", Path.home() / ".cache" / "wcl-coach"))


class WCLError(RuntimeError):
    pass


class WCLClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        site: str = "retail",
        use_cache: bool = True,
        timeout: float = 30.0,
    ) -> None:
        if site not in SITES:
            raise ValueError(f"Unknown site '{site}', choose from {sorted(SITES)}")
        self.base = SITES[site]
        self.site = site
        self.client_id = client_id
        self.client_secret = client_secret
        self.use_cache = use_cache
        self.timeout = timeout
        self.session = requests.Session()
        self._token: str | None = None
        CACHE_DIR.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ auth
    def _token_file(self) -> Path:
        h = hashlib.sha256(f"{self.site}:{self.client_id}".encode()).hexdigest()[:16]
        return CACHE_DIR / f"token-{h}.json"

    def _get_token(self) -> str:
        if self._token:
            return self._token
        tf = self._token_file()
        if tf.exists():
            try:
                data = json.loads(tf.read_text())
                if data["expires_at"] > time.time() + 60:
                    self._token = data["access_token"]
                    return self._token
            except (ValueError, KeyError):
                pass

        resp = self.session.post(
            f"{self.base}/oauth/token",
            data={"grant_type": "client_credentials"},
            auth=(self.client_id, self.client_secret),
            timeout=self.timeout,
        )
        if resp.status_code != 200:
            raise WCLError(
                f"OAuth failed ({resp.status_code}): {resp.text[:200]}. "
                "Check WCL_CLIENT_ID / WCL_CLIENT_SECRET."
            )
        data = resp.json()
        self._token = data["access_token"]
        tf.write_text(
            json.dumps(
                {"access_token": self._token, "expires_at": time.time() + data.get("expires_in", 3600)}
            )
        )
        try:
            tf.chmod(0o600)
        except OSError:
            pass
        return self._token

    # --------------------------------------------------------------- graphql
    def query(self, gql: str, variables: dict[str, Any] | None = None, cache: bool = True) -> dict:
        variables = variables or {}
        key = hashlib.sha256(
            json.dumps({"s": self.site, "q": gql, "v": variables}, sort_keys=True).encode()
        ).hexdigest()
        cache_file = CACHE_DIR / f"q-{key}.json"
        if cache and self.use_cache and cache_file.exists():
            return json.loads(cache_file.read_text())

        delay = 2.0
        for attempt in range(5):
            resp = self.session.post(
                f"{self.base}/api/v2/client",
                json={"query": gql, "variables": variables},
                headers={"Authorization": f"Bearer {self._get_token()}"},
                timeout=self.timeout,
            )
            if resp.status_code == 401 and attempt == 0:
                self._token = None
                self._token_file().unlink(missing_ok=True)
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                time.sleep(delay)
                delay *= 2
                continue
            break

        if resp.status_code != 200:
            raise WCLError(f"API error {resp.status_code}: {resp.text[:300]}")
        payload = resp.json()
        if payload.get("errors"):
            msgs = "; ".join(e.get("message", str(e)) for e in payload["errors"])
            raise WCLError(f"GraphQL error: {msgs}")

        data = payload["data"]
        if cache and self.use_cache:
            cache_file.write_text(json.dumps(data))
        return data

    def rate_limit(self) -> dict:
        q = "{ rateLimitData { limitPerHour pointsSpentThisHour pointsResetIn } }"
        return self.query(q, cache=False)["rateLimitData"]
