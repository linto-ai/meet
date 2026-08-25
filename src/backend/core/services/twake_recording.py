"""Orchestrate multi-file uploads to Twake Drive for a meet recording."""

import logging

from django.conf import settings

from core.services.twake_drive import (
    build_drive_link,
    ensure_meeting_directory,
    get_drive_token,
    save_file,
)

logger = logging.getLogger(__name__)


async def upload_recording_files(recording, owner_access, files):
    """Upload multiple files to Twake Drive under a single Reunion_{date} folder.

    Each file in `files` is a dict with keys: "filename", "content", "content_type".
    Returns the Twake Drive link if at least one file was uploaded successfully,
    else None. Returns None if Twake is not configured or the owner has no sub.
    """
    cloudery_url = getattr(settings, "CLOUDERY_URL", None)
    cloudery_token = getattr(settings, "CLOUDERY_TOKEN", None)
    if not cloudery_url or not cloudery_token:
        logger.debug("Twake Drive not configured, skipping upload")
        return None

    user = getattr(owner_access, "user", None)
    sub = getattr(user, "sub", None) if user else None
    if not sub:
        logger.warning("Owner has no OIDC sub, skipping Twake upload")
        return None

    domain = getattr(settings, "TWAKE_INSTANCE_DOMAIN", "twake.linagora.com")
    instance = (
        getattr(settings, "TWAKE_DEV_INSTANCE_OVERRIDE", None) or f"{sub}.{domain}"
    )

    drive_token = await get_drive_token(cloudery_url, cloudery_token, instance)

    meeting_time = recording.created_at.strftime("%d-%m-%Y_%H-%M")
    dirname = f"Reunion_{meeting_time}"
    dir_id = await ensure_meeting_directory(instance, drive_token, dirname)

    uploaded = []
    for f in files:
        try:
            ok = await save_file(
                instance,
                drive_token,
                dir_id,
                f["filename"],
                f["content"],
                f["content_type"],
            )
            if ok:
                uploaded.append(f["filename"])
        except Exception:
            logger.exception("Failed to upload %s to Twake Drive", f.get("filename"))

    if not uploaded:
        logger.warning(
            "No file could be uploaded to Twake Drive for recording %s", recording.id
        )
        return None

    # Log the outcome explicitly: this is the only trace the screen-recording
    # path leaves behind (unlike the transcription task, which logs its own
    # summary line), and a silent success is indistinguishable from a silent
    # skip when reading production logs.
    if len(uploaded) < len(files):
        logger.warning(
            "Partially uploaded %d/%d file(s) to Twake Drive for recording %s: %s",
            len(uploaded),
            len(files),
            recording.id,
            ", ".join(uploaded),
        )
    else:
        logger.info(
            "Uploaded %d file(s) to Twake Drive for recording %s: %s",
            len(uploaded),
            recording.id,
            ", ".join(uploaded),
        )

    return build_drive_link(instance, dir_id)
