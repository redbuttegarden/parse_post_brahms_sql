#!/usr/bin/env python3
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, Tuple

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from peewee import (
    AutoField,
    CharField,
    DateTimeField,
    IntegerField,
    Model,
    SqliteDatabase,
    TextField,
)

log = logging.getLogger("speciesimage_flow_sync")


# -----------------------------
# SQLite + Peewee persistence
# -----------------------------

DB_PATH = os.environ.get("STATE_DB_PATH", "speciesimage_state.sqlite3")
db = SqliteDatabase(DB_PATH)


class BaseModel(Model):
    class Meta:
        database = db


class SpeciesImageProcessing(BaseModel):
    """
    Tracks per SpeciesImage.id processing outcome.

    status:
      - pending: created but not attempted (rare; mostly for debugging)
      - flow_ok: flow call succeeded and we computed a description (DRY_RUN can stop here)
      - updated: PATCH to Django succeeded (this is what we skip on future runs)
      - skipped: intentionally skipped (e.g., missing image_url or no description returned)
      - failed: any exception or HTTP error during flow or patch
    """

    id = AutoField()
    species_image_id = IntegerField(unique=True, index=True)

    status = CharField(max_length=20, default="pending")
    attempts = IntegerField(default=0)

    last_flow_status_code = IntegerField(null=True)
    last_patch_status_code = IntegerField(null=True)

    # What we wrote (or would write)
    description_written = TextField(null=True)

    # Raw Power Automate response (JSON string)
    flow_response_json = TextField(null=True)

    # Error text if failed
    last_error = TextField(null=True)

    created_at = DateTimeField(default=lambda: datetime.now(timezone.utc))
    updated_at = DateTimeField(default=lambda: datetime.now(timezone.utc))


def init_db() -> None:
    db.connect(reuse_if_open=True)
    db.create_tables([SpeciesImageProcessing])


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# -----------------------------
# HTTP helpers
# -----------------------------


def build_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=5,
        connect=5,
        read=5,
        status=5,
        backoff_factor=0.8,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "POST", "PATCH"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=20, pool_maxsize=20)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    s.headers.update(
        {"Accept": "application/json", "User-Agent": "speciesimage-flow-sync/2.0"}
    )
    return s


def iter_drf_results(
    session: requests.Session,
    url: str,
    headers: Dict[str, str],
    timeout: float,
) -> Iterator[Dict[str, Any]]:
    """
    Supports DRF pagination (next/results) and non-paginated list responses.
    """
    while url:
        r = session.get(url, headers=headers, timeout=timeout)
        if r.status_code >= 400:
            raise RuntimeError(f"GET {url} failed: {r.status_code} {r.text[:400]}")
        data = r.json()

        if isinstance(data, dict) and "results" in data:
            for item in data.get("results") or []:
                yield item
            url = data.get("next") or ""
        elif isinstance(data, list):
            for item in data:
                yield item
            url = ""
        else:
            raise RuntimeError(
                f"Unexpected list response shape from {url}: {type(data)}"
            )


def call_flow(
    session: requests.Session,
    flow_url: str,
    *,
    species_full_name: str,
    image_url: str,
    timeout: float,
) -> Tuple[int, Dict[str, Any]]:
    """
    Flow request schema:
      {
        "speciesFullName": "string",
        "imageUrl": "string"
      }

    Flow response schema:
      { "description": "..." }
    """
    payload = {
        "speciesFullName": species_full_name,
        "imageUrl": image_url,
    }

    r = session.post(flow_url, json=payload, timeout=timeout)
    status = r.status_code
    if status >= 400:
        raise RuntimeError(f"Flow call failed: {status} {r.text[:500]}")

    try:
        data = r.json()
    except ValueError:
        raise RuntimeError(
            f"Flow did not return JSON. Status={status} Body={r.text[:300]}"
        )

    if not isinstance(data, dict) or "description" not in data:
        raise RuntimeError(f"Flow response missing 'description'. Got: {data}")

    if not isinstance(data["description"], str) or not data["description"].strip():
        raise RuntimeError("Flow returned an empty description.")

    return status, data


def patch_description(
    session: requests.Session,
    patch_url_template: str,
    headers: Dict[str, str],
    species_image_id: int,
    description: str,
    timeout: float,
) -> Tuple[int, Dict[str, Any]]:
    url = patch_url_template.format(id=species_image_id)
    r = session.patch(
        url,
        headers={**headers, "Content-Type": "application/json"},
        data=json.dumps({"description_write": description}),
        timeout=timeout,
    )
    status = r.status_code
    if status >= 400:
        raise RuntimeError(f"PATCH {url} failed: {status} {r.text[:500]}")
    return status, r.json()


# -----------------------------
# Mapping flow output -> description
# -----------------------------


def build_description(flow_result: Dict[str, Any]) -> str:
    """
    Flow returns only {"description": "..."}.
    """
    return flow_result["description"].strip()


# -----------------------------
# State helpers (Peewee)
# -----------------------------


def get_or_create_state(species_image_id: int) -> SpeciesImageProcessing:
    obj, _created = SpeciesImageProcessing.get_or_create(
        species_image_id=species_image_id
    )
    return obj


def should_skip(
    species_image_id: int, *, skip_updated: bool, reprocess_failed: bool
) -> bool:
    try:
        row = SpeciesImageProcessing.get(
            SpeciesImageProcessing.species_image_id == species_image_id
        )
    except SpeciesImageProcessing.DoesNotExist:
        return False

    if skip_updated and row.status == "updated":
        return True

    if not reprocess_failed and row.status == "failed":
        return True

    return False


# -----------------------------
# Main
# -----------------------------


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s"
    )

    init_db()

    api_list_url = os.environ["API_LIST_URL"]
    api_patch_url = os.environ.get(
        "API_PATCH_URL_TEMPLATE",
        "https://redbuttegarden.org/plants/api/species-images/{id}/image-description/",
    )
    flow_url = os.environ["POWER_AUTOMATE_URL"]

    auth_scheme = os.environ.get("API_AUTH_SCHEME", "Bearer")
    api_token = os.environ["API_TOKEN"]
    headers = {"Authorization": f"{auth_scheme} {api_token}"}

    timeout = float(os.environ.get("TIMEOUT_SECONDS", "30"))
    rate_limit = float(os.environ.get("RATE_LIMIT_SECONDS", "0.0"))

    dry_run = os.environ.get("DRY_RUN", "0") == "1"
    skip_if_has_description = os.environ.get("SKIP_IF_HAS_DESCRIPTION", "1") == "1"

    # State policy
    skip_updated = os.environ.get("SKIP_UPDATED", "1") == "1"
    reprocess_failed = os.environ.get("REPROCESS_FAILED", "1") == "1"

    session = build_session()

    processed = updated = skipped = failed = 0

    for item in iter_drf_results(session, api_list_url, headers, timeout):
        sid = item.get("id")
        if not isinstance(sid, int):
            log.warning("Skipping item without integer id: %r", sid)
            skipped += 1
            continue

        if should_skip(
            sid, skip_updated=skip_updated, reprocess_failed=reprocess_failed
        ):
            log.info("Skipping %s (already processed per DB policy).", sid)
            skipped += 1
            continue

        image_url = item.get("image_url")
        species_full_name = item.get("species_full_name")
        current_desc = item.get("image_description")

        state = get_or_create_state(sid)
        state.attempts += 1
        state.updated_at = utcnow()
        state.last_error = None
        state.save()

        # Optional: skip if already has description in API payload
        if (
            skip_if_has_description
            and isinstance(current_desc, str)
            and current_desc.strip()
        ):
            state.status = "skipped"
            state.last_error = "Skipped because image already has a description (SKIP_IF_HAS_DESCRIPTION=1)."
            state.updated_at = utcnow()
            state.save()
            log.info("Skipping %s: already has description.", sid)
            skipped += 1
            continue

        if not image_url:
            state.status = "skipped"
            state.last_error = "Missing image_url in list payload."
            state.updated_at = utcnow()
            state.save()
            log.warning("Skipping %s: missing image_url.", sid)
            skipped += 1
            continue

        try:
            # speciesFullName is required by the flow schema
            if not isinstance(species_full_name, str) or not species_full_name.strip():
                state.status = "skipped"
                state.last_error = "Missing species_full_name in list payload (required by flow schema)."
                state.updated_at = utcnow()
                state.save()
                log.warning("Skipping %s: missing species_full_name.", sid)
                skipped += 1
                continue

            flow_status, flow_result = call_flow(
                session,
                flow_url,
                species_full_name=species_full_name.strip(),
                image_url=image_url,
                timeout=timeout,
            )

            state.last_flow_status_code = flow_status
            state.flow_response_json = json.dumps(flow_result, ensure_ascii=False)
            state.updated_at = utcnow()
            state.save()

            new_desc = build_description(flow_result)
            state.description_written = new_desc
            state.updated_at = utcnow()
            state.save()

            if dry_run:
                state.status = "flow_ok"
                state.updated_at = utcnow()
                state.save()
                log.info("[DRY RUN] Would PATCH %s description=%r", sid, new_desc)
                skipped += 1
            else:
                patch_status, resp = patch_description(
                    session, api_patch_url, headers, sid, new_desc, timeout
                )
                state.last_patch_status_code = patch_status
                state.status = "updated"
                state.updated_at = utcnow()
                state.save()

                log.info(
                    "Updated %s -> image_id=%s species=%s",
                    sid,
                    resp.get("image_id"),
                    resp.get("species_full_name"),
                )
                updated += 1

            processed += 1

        except Exception as e:
            state.status = "failed"
            state.last_error = str(e)
            state.updated_at = utcnow()
            state.save()

            failed += 1
            log.exception("Failed item %s: %s", sid, e)

        if rate_limit > 0:
            time.sleep(rate_limit)

    log.info(
        "Done. processed=%d updated=%d skipped=%d failed=%d db=%s",
        processed,
        updated,
        skipped,
        failed,
        DB_PATH,
    )
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    main()
