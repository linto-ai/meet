"""Tests for core.services.audio_extract."""

import subprocess
from unittest import mock

import pytest

from core.services.audio_extract import extract_audio_from_video


class TestExtractAudioFromVideo:
    def test_unsupported_format_raises(self):
        with pytest.raises(ValueError, match="Unsupported"):
            extract_audio_from_video(b"data", output_format="xyz")

    def test_ffmpeg_missing_raises_runtime_error(self, monkeypatch):
        def fake_run(*args, **kwargs):
            raise FileNotFoundError("ffmpeg not found")

        monkeypatch.setattr(subprocess, "run", fake_run)
        with pytest.raises(RuntimeError, match="ffmpeg binary not found"):
            extract_audio_from_video(b"fake mp4 bytes")

    def test_corrupted_input_raises_called_process_error(self, monkeypatch):
        def fake_run(*args, **kwargs):
            raise subprocess.CalledProcessError(
                returncode=1, cmd=args[0], stderr=b"Invalid data"
            )

        monkeypatch.setattr(subprocess, "run", fake_run)
        with pytest.raises(subprocess.CalledProcessError):
            extract_audio_from_video(b"not a valid mp4")

    def test_timeout_propagates(self, monkeypatch):
        def fake_run(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd=args[0], timeout=600)

        monkeypatch.setattr(subprocess, "run", fake_run)
        with pytest.raises(subprocess.TimeoutExpired):
            extract_audio_from_video(b"huge mp4")

    def test_timeout_is_applied(self, monkeypatch):
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["check"] = kwargs.get("check")
            captured["timeout"] = kwargs.get("timeout")
            # Simulate ffmpeg by writing bytes to the output path (last arg)
            with open(cmd[-1], "wb") as f:
                f.write(b"OggS fake audio")
            return mock.Mock(returncode=0)

        monkeypatch.setattr(subprocess, "run", fake_run)
        result = extract_audio_from_video(b"fake mp4")

        assert result == b"OggS fake audio"
        assert captured["check"] is True
        assert captured["timeout"] == 600
        assert "-vn" in captured["cmd"]
        assert "-acodec" in captured["cmd"]
        assert "libvorbis" in captured["cmd"]

    def test_wav_output_uses_pcm_codec(self, monkeypatch):
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            with open(cmd[-1], "wb") as f:
                f.write(b"RIFF wav")
            return mock.Mock(returncode=0)

        monkeypatch.setattr(subprocess, "run", fake_run)
        extract_audio_from_video(b"fake mp4", output_format="wav")

        assert "pcm_s16le" in captured["cmd"]

    def test_copy_output_streams_without_reencoding(self, monkeypatch):
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            with open(cmd[-1], "wb") as f:
                f.write(b"fake m4a")
            return mock.Mock(returncode=0)

        monkeypatch.setattr(subprocess, "run", fake_run)
        result = extract_audio_from_video(b"fake mp4", output_format="copy")

        assert result == b"fake m4a"
        assert "-c:a" in captured["cmd"]
        assert "copy" in captured["cmd"]
        assert "ipod" in captured["cmd"]
        assert captured["cmd"][-1].endswith(".m4a")
        assert "-acodec" not in captured["cmd"]
