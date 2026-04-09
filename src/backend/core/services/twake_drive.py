"""Twake Drive integration via Cloudery + Cozy Stack files API.

Ported from meet2twake (Go) to Python async.
Reference: /home/realitix/git/linagora/meet2twake/main.go
"""

import json
import logging

import aiohttp

logger = logging.getLogger(__name__)

MEETINGS_DIR_NAME = "_Reunions"


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


async def ensure_meeting_directory(instance, token, dirname):
    """Create _Reunions/{dirname} directory structure.

    Ref: meet2twake ensureMeetingDirectory (lines 503-509)
    """
    meetings_dir_id = await ensure_directory(
        instance,
        token,
        f"/{MEETINGS_DIR_NAME}",
        "io.cozy.files.root-dir",
        favorite=True,
    )
    return await ensure_directory(
        instance,
        token,
        f"/{MEETINGS_DIR_NAME}/{dirname}",
        meetings_dir_id,
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
