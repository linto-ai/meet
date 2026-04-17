"""Extract audio track from a video using ffmpeg."""

import logging
import os
import subprocess
import tempfile

logger = logging.getLogger(__name__)

CODEC_MAP = {
    "ogg": "libvorbis",
    "wav": "pcm_s16le",
    "mp3": "libmp3lame",
}


def extract_audio_from_video(video_bytes: bytes, output_format: str = "ogg") -> bytes:
    """Extract audio track from a video and return the audio bytes.

    Writes video_bytes to a temp file, runs ffmpeg to extract the audio
    stream to the requested format, reads back and returns the audio bytes.
    Raises RuntimeError if ffmpeg is missing, CalledProcessError if ffmpeg
    fails (corrupted input), TimeoutExpired if the job takes too long,
    ValueError for unsupported output formats.
    """
    if output_format not in CODEC_MAP:
        raise ValueError(f"Unsupported output_format: {output_format}")

    acodec = CODEC_MAP[output_format]

    input_file = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
    input_path = input_file.name
    try:
        input_file.write(video_bytes)
        input_file.flush()
    finally:
        input_file.close()

    output_file = tempfile.NamedTemporaryFile(
        suffix=f".{output_format}", delete=False
    )
    output_path = output_file.name
    output_file.close()

    try:
        try:
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-i",
                    input_path,
                    "-vn",
                    "-acodec",
                    acodec,
                    "-f",
                    output_format,
                    output_path,
                ],
                check=True,
                capture_output=True,
                timeout=600,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                "ffmpeg binary not found. Install ffmpeg in the runtime environment."
            ) from exc
        except subprocess.CalledProcessError as exc:
            logger.error(
                "ffmpeg failed: %s",
                exc.stderr.decode(errors="replace") if exc.stderr else "",
            )
            raise

        with open(output_path, "rb") as f:
            return f.read()
    finally:
        for path in (input_path, output_path):
            try:
                os.unlink(path)
            except OSError:
                pass
