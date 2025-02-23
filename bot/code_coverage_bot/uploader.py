# -*- coding: utf-8 -*-
import itertools
import os.path

import requests
import structlog
import tenacity
import zstandard as zstd
from google.cloud.storage.bucket import Bucket
from requests import HTTPError

from code_coverage_bot.secrets import secrets
from code_coverage_tools.gcp import get_bucket

logger = structlog.get_logger(__name__)
GCP_COVDIR_PATH = "{repository}/{revision}/{platform}:{suite}.json.zstd"


def gcp(repository, revision, report, platform, suite):
    """
    Upload a grcov raw report on Google Cloud Storage
    * Compress with zstandard
    * Upload on bucket using revision in name
    * Trigger ingestion on channel's backend
    """
    assert isinstance(report, bytes)
    assert isinstance(platform, str)
    assert isinstance(suite, str)
    bucket = get_bucket(secrets[secrets.GOOGLE_CLOUD_STORAGE])

    # Compress report
    compressor = zstd.ZstdCompressor(threads=-1)
    archive = compressor.compress(report)

    # Upload archive
    path = GCP_COVDIR_PATH.format(
        repository=repository, revision=revision, platform=platform, suite=suite
    )
    blob = bucket.blob(path)
    blob.upload_from_string(archive)

    # Update headers
    blob.content_type = "application/json"
    blob.content_encoding = "zstd"
    blob.patch()

    logger.info("Uploaded {} on {}".format(path, bucket))

    try:
        # Trigger ingestion on backend
        gcp_ingest(repository, revision, platform, suite)
    except HTTPError as e:
        logger.warn(f"Failed to ingest report. {e}")

    return blob


def gcp_zero_coverage(report):
    """
    Upload a grcov a zero coverage report on Google Cloud Storage
    * Compress with zstandard
    * Upload in the main bucket directory
    """
    assert isinstance(report, bytes)
    bucket = get_bucket(secrets[secrets.GOOGLE_CLOUD_STORAGE])

    # Compress report
    compressor = zstd.ZstdCompressor(threads=-1)
    archive = compressor.compress(report)

    # Upload archive (this should be in the base directory, because we only care about the latest report)
    path = "zero_coverage_report.json.zstd"
    blob = bucket.blob(path)
    blob.upload_from_string(archive)

    # Update headers
    blob.content_type = "application/json"
    blob.content_encoding = "zstd"
    blob.patch()

    logger.info("Uploaded {} on {}".format(path, bucket))

    return blob


def gcp_covdir_exists(
    bucket: Bucket, repository: str, revision: str, platform: str, suite: str
) -> bool:
    """
    Check if a covdir report exists on the Google Cloud Storage bucket
    """
    path = GCP_COVDIR_PATH.format(
        repository=repository, revision=revision, platform=platform, suite=suite
    )
    blob = bucket.blob(path)
    return blob.exists()


@tenacity.retry(
    stop=tenacity.stop_after_attempt(10),
    wait=tenacity.wait_exponential(multiplier=1, min=16, max=64),
    reraise=True,
)
def gcp_ingest(repository, revision, platform, suite):
    """
    The GCP report ingestion is triggered remotely on a backend
    by making a simple HTTP request on the /v2/path endpoint
    By specifying the exact new revision processed, the backend
    will download automatically the new report.
    """
    params = {"repository": repository, "changeset": revision}
    if platform:
        params["platform"] = platform
    if suite:
        params["suite"] = suite
    backend_host = secrets[secrets.BACKEND_HOST]
    logger.info(
        "Ingesting report on backend",
        host=backend_host,
        repository=repository,
        revision=revision,
        platform=platform,
        suite=suite,
    )
    resp = requests.get("{}/v2/path".format(backend_host), params=params)
    resp.raise_for_status()
    logger.info("Successfully ingested report on backend !")
    return resp


def gcp_latest(repository):
    """
    List the latest reports ingested on the backend
    """
    params = {"repository": repository}
    backend_host = secrets[secrets.BACKEND_HOST]
    resp = requests.get("{}/v2/latest".format(backend_host), params=params)
    resp.raise_for_status()
    return resp.json()


def covdir_paths(report):
    """
    Load a covdir report and recursively list all the paths
    """
    assert isinstance(report, dict)

    def _extract(obj, base_path=""):
        out = []
        children = obj.get("children", {})
        if children:
            # Recursive on folder files
            out += itertools.chain(
                *[
                    _extract(child, os.path.join(base_path, obj["name"]))
                    for child in children.values()
                ]
            )

        else:
            # Add full filename
            out.append(os.path.join(base_path, obj["name"]))

        return out

    return _extract(report)
