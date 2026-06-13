#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Thin REST client for the BudgetBakers Wallet API.

Wraps the handful of endpoints wallet-autocapture needs:

  * ``GET``  helpers (``get``) with a Bearer token,
  * ``GET /records`` with transparent offset pagination,
  * ``POST /records`` in batches of 20 (create),
  * ``PATCH /records`` in batches of 10 (update existing),
  * ``DELETE /records`` in batches of 10 (used by the undo flow),
  * reference lookups for accounts, labels and categories (all paginated).

Batch sizes mirror the API's documented per-request limits. A clear,
actionable message is raised on ``401`` (expired / wrong token) so the caller
can tell the user to regenerate the token rather than surfacing a raw HTTP
error. Only the Python standard library is used.
"""
import json
import urllib.error
import urllib.parse
import urllib.request


# Per-request batch limits documented by the Wallet API.
_CREATE_BATCH = 20   # POST /records
_PATCH_BATCH = 10    # PATCH /records
_DELETE_BATCH = 10   # DELETE /records
_PAGE_SIZE = 200     # GET pagination page size
_TIMEOUT = 40        # seconds per HTTP call


class WalletAuthError(Exception):
    """Raised on HTTP 401 -- the token is missing, expired, or invalid."""


class WalletAPIError(Exception):
    """Raised on any other non-2xx HTTP response from the Wallet API."""


class Wallet:
    """Minimal authenticated client for the BudgetBakers Wallet REST API.

    Args:
        token: Bearer token (generated in Wallet web -> Settings -> API).
        api_base: API base URL, e.g.
            ``https://rest.budgetbakers.com/wallet/v1/api``.
    """

    def __init__(self, token, api_base):
        self.token = (token or "").strip()
        self.api_base = (api_base or "").rstrip("/")

    # ---- low-level request plumbing -------------------------------------

    def _headers(self):
        return {
            "Authorization": "Bearer " + self.token,
            "Content-Type": "application/json",
        }

    def _request(self, path, method="GET", body=None):
        """Perform one HTTP request and return the parsed JSON body (or None).

        Raises:
            WalletAuthError: on HTTP 401.
            WalletAPIError: on any other HTTP error or transport failure.
        """
        url = self.api_base + path
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            url, data=data, method=method, headers=self._headers()
        )
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
                raw = resp.read()
                if not raw:
                    return None
                return json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:400]
            except Exception:
                pass
            if e.code == 401:
                raise WalletAuthError(
                    "Wallet API returned 401 Unauthorized. Your token is "
                    "missing, expired, or invalid. Regenerate it in Wallet web "
                    "-> Settings -> API and update your token file / "
                    "WALLET_API_TOKEN environment variable."
                ) from e
            raise WalletAPIError(
                f"Wallet API {method} {path} failed: HTTP {e.code} -- {detail}"
            ) from e
        except urllib.error.URLError as e:
            raise WalletAPIError(
                f"Wallet API {method} {path} failed (network): {e.reason}"
            ) from e

    @staticmethod
    def _unwrap(data, key):
        """Pull a list out of a response that may be a bare list or wrapped.

        The API sometimes returns ``{"records": [...]}`` and sometimes a bare
        ``[...]``. This normalizes both to a list.
        """
        if isinstance(data, dict):
            return data.get(key, []) or []
        return data or []

    # ---- generic GET ----------------------------------------------------

    def get(self, path):
        """GET ``path`` (relative to ``api_base``) and return the parsed body.

        Args:
            path: Path beginning with ``/`` (e.g. ``"/accounts"``). Any query
                string must already be included.
        """
        return self._request(path, method="GET")

    # ---- records --------------------------------------------------------

    def get_records(self, params):
        """GET /records across all pages, returning a flat list.

        Pagination follows the API's ``nextOffset`` when present, and otherwise
        stops once a short page (< page size) is returned. ``limit`` and
        ``offset`` in ``params`` are managed internally and overwritten.

        Args:
            params: dict of query parameters, e.g.
                ``{"recordDate": "gte.2026-01-01T00:00:00Z", "sortBy":
                "-recordDate"}``. Values may be repeated by passing a list.

        Returns:
            list: All record dicts in the queried range.
        """
        out = []
        offset = 0
        params = dict(params or {})
        while True:
            page_params = dict(params)
            page_params["limit"] = _PAGE_SIZE
            page_params["offset"] = offset
            query = urllib.parse.urlencode(page_params, doseq=True)
            data = self._request("/records?" + query, method="GET")
            records = self._unwrap(data, "records")
            out.extend(records)
            next_offset = data.get("nextOffset") if isinstance(data, dict) else None
            # Stop on: empty page, no/duplicate nextOffset, or a short page.
            if not records or next_offset in (None, offset) or len(records) < _PAGE_SIZE:
                break
            offset = next_offset
        return out

    def create_records(self, records):
        """POST /records in batches of 20. Returns an aggregated summary.

        Args:
            records: list of WALLET RECORD dicts ready to create.

        Returns:
            dict: ``{"succeeded": int, "failed": int, "errors": [str, ...]}``.

        Raises:
            WalletAuthError / WalletAPIError: propagated from the first failing
            batch (the caller decides whether to stop).
        """
        return self._send_in_batches(records, _CREATE_BATCH, method="POST")

    def patch_records(self, records):
        """PATCH /records in batches of 10. Returns an aggregated summary.

        Args:
            records: list of partial WALLET RECORD dicts; each must carry the
                record ``id`` plus the fields to change.

        Returns:
            dict: ``{"succeeded": int, "failed": int, "errors": [str, ...]}``.
        """
        return self._send_in_batches(records, _PATCH_BATCH, method="PATCH")

    def delete_records(self, ids):
        """DELETE /records in batches of 10, by id. Returns a summary.

        The body shape is ``{"ids": [...]}`` per the API. Used by the undo
        flow to remove auto-captured records.

        Args:
            ids: list of record id strings to delete.

        Returns:
            dict: ``{"succeeded": int, "failed": int, "errors": [str, ...]}``.
        """
        summary = {"succeeded": 0, "failed": 0, "errors": []}
        for i in range(0, len(ids), _DELETE_BATCH):
            batch = ids[i:i + _DELETE_BATCH]
            self._request("/records", method="DELETE", body={"ids": batch})
            # DELETE returns 2xx with no per-item summary; count the batch.
            summary["succeeded"] += len(batch)
        return summary

    def _send_in_batches(self, records, batch_size, method):
        """Send ``records`` as a flat array body in ``batch_size`` chunks.

        Aggregates the API's per-batch ``summary`` block when present, and
        otherwise assumes the whole batch succeeded (the API returns 2xx).
        """
        summary = {"succeeded": 0, "failed": 0, "errors": []}
        for i in range(0, len(records), batch_size):
            batch = records[i:i + batch_size]
            body = self._request("/records", method=method, body=batch)
            if isinstance(body, dict) and "summary" in body:
                s = body.get("summary", {}) or {}
                succeeded = s.get("succeeded", len(batch))
                failed = s.get("clientErrors", 0) + s.get("serverErrors", 0)
                summary["succeeded"] += succeeded
                summary["failed"] += failed
                for res in body.get("results", []) or []:
                    if not res.get("success"):
                        err = res.get("error", "")
                        if err:
                            summary["errors"].append(str(err)[:200])
            else:
                # No structured summary -> a 2xx means the batch went through.
                summary["succeeded"] += len(batch)
        return summary

    # ---- reference data (accounts / labels / categories) ----------------

    def get_accounts(self):
        """GET /accounts -> list of account dicts (single page; small set)."""
        return self._unwrap(self.get("/accounts?limit=%d" % _PAGE_SIZE), "accounts")

    def get_labels(self):
        """GET /labels across pages -> list of label dicts."""
        return self._paginate_reference("/labels", "labels")

    def get_categories(self):
        """GET /categories across pages -> list of category dicts."""
        return self._paginate_reference("/categories", "categories")

    def _paginate_reference(self, path, key):
        """Page through a reference endpoint that supports limit/offset."""
        out = []
        offset = 0
        while True:
            data = self.get(f"{path}?limit={_PAGE_SIZE}&offset={offset}")
            items = self._unwrap(data, key)
            out.extend(items)
            next_offset = data.get("nextOffset") if isinstance(data, dict) else None
            if not items or next_offset in (None, offset) or len(items) < _PAGE_SIZE:
                break
            offset = next_offset
        return out
