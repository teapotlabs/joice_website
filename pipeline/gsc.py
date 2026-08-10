"""Thin Google Search Console client for the SEO pipeline.

Auth: a Google Cloud service account whose email has been added as a user
on the GSC property. The JSON key arrives via the GSC_SERVICE_ACCOUNT_JSON
environment variable (GitHub Actions secret).

Only two endpoints are used:
  - Search Analytics query (clicks/impressions/CTR/position by dimension)
  - URL Inspection (is this URL actually indexed?)
"""

import json
import os
from urllib.parse import quote

import requests
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import service_account

SCOPES = ["https://www.googleapis.com/auth/webmasters.readonly"]
INSPECT_SCOPES = ["https://www.googleapis.com/auth/webmasters"]


def _credentials(scopes):
    raw = os.environ.get("GSC_SERVICE_ACCOUNT_JSON")
    if not raw:
        raise RuntimeError("GSC_SERVICE_ACCOUNT_JSON is not set")
    creds = service_account.Credentials.from_service_account_info(
        json.loads(raw), scopes=scopes)
    creds.refresh(GoogleAuthRequest())
    return creds


class GscClient:
    def __init__(self, site):
        self.site = site
        self._token = _credentials(SCOPES).token
        # URL Inspection needs the full (non-readonly) scope.
        self._inspect_token = _credentials(INSPECT_SCOPES).token

    def query(self, start_date, end_date, dimensions, page_filter=None,
              row_limit=1000):
        """Search Analytics rows: [{keys, clicks, impressions, ctr, position}]."""
        body = {
            "startDate": start_date,
            "endDate": end_date,
            "dimensions": dimensions,
            "rowLimit": row_limit,
        }
        if page_filter:
            body["dimensionFilterGroups"] = [{
                "filters": [{"dimension": "page", "operator": "contains",
                             "expression": page_filter}],
            }]
        r = requests.post(
            "https://www.googleapis.com/webmasters/v3/sites/{}/searchAnalytics/query"
            .format(quote(self.site, safe="")),
            headers={"Authorization": "Bearer " + self._token},
            json=body, timeout=60,
        )
        r.raise_for_status()
        return r.json().get("rows", [])

    def inspect(self, url):
        """URL Inspection verdict for one URL. Returns (indexed, coverage)."""
        r = requests.post(
            "https://searchconsole.googleapis.com/v1/urlInspection/index:inspect",
            headers={"Authorization": "Bearer " + self._inspect_token},
            json={"inspectionUrl": url, "siteUrl": self.site},
            timeout=60,
        )
        r.raise_for_status()
        result = r.json().get("inspectionResult", {}).get("indexStatusResult", {})
        verdict = result.get("verdict", "VERDICT_UNSPECIFIED")
        coverage = result.get("coverageState", "unknown")
        return verdict == "PASS", coverage
