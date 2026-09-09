"""Twake Drive integration via Cloudery + Cozy Stack files API.

Ported from meet2twake (Go) to Python async.
Reference: /home/realitix/git/linagora/meet2twake/main.go
"""

import json
import logging
from urllib.parse import quote

import aiohttp

logger = logging.getLogger(__name__)

ROOT_DIR_ID = "io.cozy.files.root-dir"
TRASH_PATH_PREFIX = "/.cozy_trash"

MEETINGS_DIR_NAME = "_Meetings"

# "Magic folder" reference, following the cozy-client convention
# (`io.cozy.apps/administrative`, `io.cozy.apps/notes`, ...). The referenced
# `io.cozy.apps` document does not exist: the stack only stores the pair
# (type, id) in the folder's `referenced_by` array. Looking the folder up by
# this reference instead of by path keeps working after the user renames or
# moves it.
MEETINGS_DIR_REFERENCE = {"type": "io.cozy.apps", "id": "io.cozy.apps/meet"}

MEETING_DATE_FORMAT = "%Y %m %d"
MEETING_DATETIME_FORMAT = "%Y %m %d %H%M"


def meeting_date(recording):
    """Date prefix of the files uploaded for a meeting."""
    return recording.created_at.strftime(MEETING_DATE_FORMAT)


def build_meeting_dirname(recording):
    """Name of the per-meeting folder: `Meeting - {date} {time} - {room id}`.

    The time keeps two recordings of the same room on the same day apart.
    """
    started_at = recording.created_at.strftime(MEETING_DATETIME_FORMAT)
    return f"Meeting - {started_at} - {recording.room_id}"


async def get_drive_token(cloudery_url, cloudery_token, instance):
    """Get a drive token for the given instance from the Cloudery.

    Ref: meet2twake getDriveToken (lines 707-734)
    POST {cloudery_url}/api/public/instances/{instance}/drive_token
    """
    url = f"{cloudery_url.rstrip('/')}/api/public/instances/{instance}/drive_token"
    headers = {"Authorization": f"Bearer {cloudery_token}"}

    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(
                    f"Cloudery drive_token failed for {instance}: "
                    f"{resp.status} {body[:500]}"
                )
            data = await resp.json()
            return data["token"]


async def get_dir_id(instance, token, path):
    """Get the ID of a directory by path, or None if it doesn't exist.

    Ref: meet2twake getDirID (lines 545-575)
    GET https://{instance}/files/metadata?Path={path}
    """
    url = f"https://{instance}/files/metadata?Path={path}"
    headers = {"Authorization": f"Bearer {token}"}

    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as resp:
            if resp.status == 404:
                return None
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(
                    f"Cozy get_dir_id failed for {path}: {resp.status} {body[:500]}"
                )
            data = await resp.json()
            return data["data"]["id"]


async def create_directory(instance, token, dirname, parent_id):
    """Create a directory and return its ID.

    Ref: meet2twake createDirectory (lines 577-609)
    POST https://{instance}/files/{parent_id}?Type=directory&Name={dirname}
    """
    url = f"https://{instance}/files/{parent_id}?Type=directory&Name={dirname}"
    headers = {"Authorization": f"Bearer {token}"}

    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers) as resp:
            if resp.status != 201:
                body = await resp.text()
                raise RuntimeError(
                    f"Cozy create_directory failed for {dirname}: "
                    f"{resp.status} {body[:500]}"
                )
            data = await resp.json()
            return data["data"]["id"]


async def put_in_favorite(instance, token, dir_id):
    """Mark a directory as favorite.

    Ref: meet2twake putInFavorite (lines 611-646)
    PATCH https://{instance}/files/{dir_id}
    """
    url = f"https://{instance}/files/{dir_id}"
    headers = {"Authorization": f"Bearer {token}"}
    payload = {
        "data": {
            "type": "io.cozy.files",
            "id": dir_id,
            "attributes": {
                "cozyMetadata": {
                    "favorite": True,
                },
            },
        },
    }

    async with aiohttp.ClientSession() as session:
        async with session.patch(
            url, headers=headers, data=json.dumps(payload)
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                logger.warning(
                    "Cannot put in favorite %s: %s %s",
                    dir_id,
                    resp.status,
                    body[:500],
                )


async def ensure_directory(instance, token, path, parent_id, favorite=False):
    """Ensure a directory exists, creating it if needed.

    Ref: meet2twake ensureDirectory (lines 519-537)
    """
    dir_id = await get_dir_id(instance, token, path)
    if dir_id:
        return dir_id

    dirname = path.rsplit("/", 1)[-1]
    dir_id = await create_directory(instance, token, dirname, parent_id)
    if favorite:
        await put_in_favorite(instance, token, dir_id)
    return dir_id


def _references_url(instance, reference):
    """Build the stack URL for the references of a document.

    The document id may contain a `/` (e.g. `io.cozy.apps/meet`), so it has to
    be percent-encoded as a single path segment; the stack unescapes it.
    """
    doc_id = quote(reference["id"], safe="")
    return (
        f"https://{instance}/data/{reference['type']}/{doc_id}/relationships/references"
    )


def pick_live_directory(included):
    """Return the most recently created non-trashed directory, or None.

    Port of cozy-client `getReferencedFolder`: the stack view does not filter
    out trashed files, so this is done here.
    """
    directories = [
        doc
        for doc in included
        if doc.get("type") == "io.cozy.files"
        and doc.get("attributes", {}).get("type") == "directory"
        and not doc["attributes"].get("path", "").startswith(TRASH_PATH_PREFIX)
    ]
    if not directories:
        return None
    return max(directories, key=lambda doc: doc["attributes"].get("created_at", ""))


async def get_referenced_directory(instance, token, reference):
    """Return {"id", "path"} of the directory referenced by `reference`, or None.

    Port of cozy-client `getReferencedFolder`.
    GET https://{instance}/data/{type}/{id}/relationships/references?include=files
    """
    url = f"{_references_url(instance, reference)}?include=files"
    headers = {"Authorization": f"Bearer {token}"}

    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(
                    f"Cozy get_referenced_directory failed for {reference['id']}: "
                    f"{resp.status} {body[:500]}"
                )
            data = await resp.json()

    directory = pick_live_directory(data.get("included", []))
    if directory is None:
        return None
    return {"id": directory["id"], "path": directory["attributes"]["path"]}


async def add_reference(instance, token, reference, dir_id):
    """Add `reference` to the `referenced_by` of the given directory.

    Port of cozy-stack-client `addReferencesTo`.
    POST https://{instance}/data/{type}/{id}/relationships/references
    """
    url = _references_url(instance, reference)
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/vnd.api+json",
    }
    payload = {"data": [{"type": "io.cozy.files", "id": dir_id}]}

    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, data=json.dumps(payload)) as resp:
            if resp.status != 204:
                body = await resp.text()
                raise RuntimeError(
                    f"Cozy add_reference failed for {dir_id}: "
                    f"{resp.status} {body[:500]}"
                )


async def ensure_meetings_directory(instance, token):
    """Return {"id", "path"} of the meetings "magic folder", creating it if needed.

    Port of cozy-client `ensureMagicFolder`: look the folder up by reference
    first, so a renamed or moved folder is still found. Fall back to the
    default path (which also adopts a pre-existing, unreferenced `_Meetings`
    folder), and tag it with the reference.
    """
    directory = await get_referenced_directory(instance, token, MEETINGS_DIR_REFERENCE)
    if directory:
        return directory

    path = f"/{MEETINGS_DIR_NAME}"
    dir_id = await ensure_directory(instance, token, path, ROOT_DIR_ID, favorite=True)
    await add_reference(instance, token, MEETINGS_DIR_REFERENCE, dir_id)
    return {"id": dir_id, "path": path}


async def ensure_meeting_directory(instance, token, recording):
    """Create {meetings folder}/{meeting folder} and return the latter's id.

    Ref: meet2twake ensureMeetingDirectory (lines 503-509)
    """
    meetings_dir = await ensure_meetings_directory(instance, token)
    return await ensure_directory(
        instance,
        token,
        f"{meetings_dir['path']}/{build_meeting_dirname(recording)}",
        meetings_dir["id"],
    )


async def save_file(  # noqa: PLR0913
    instance, token, dir_id, filename, content, content_type
):
    """Upload a file to the given directory.

    Ref: meet2twake saveTranscript (lines 762-808) / saveSummary (lines 810-839)
    POST https://{instance}/files/{dir_id}?Type=file&Name={filename}&Content-Type={ct}
    """
    url = (
        f"https://{instance}/files/{dir_id}"
        f"?Type=file&Name={filename}&Content-Type={content_type}"
    )
    headers = {"Authorization": f"Bearer {token}"}

    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, data=content) as resp:
            if resp.status != 201:
                body = await resp.text()
                logger.error(
                    "Cozy save_file failed for %s: %s %s",
                    filename,
                    resp.status,
                    body[:500],
                )
                return False
            return True


def build_drive_link(instance, dir_id):
    """Build the Twake Drive frontend URL for a folder.

    Ref: meet2twake sendEmail (lines 844-846)
    instance "sub.twake.linagora.com" → "sub-drive.twake.linagora.com"
    """
    parts = instance.split(".")
    parts[0] += "-drive"
    return f"https://{'.'.join(parts)}/#/folder/{dir_id}"
