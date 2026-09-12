from __future__ import annotations

import asyncio
import io
import struct
import threading
import types
import wave
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from api import agentlink, main, musiclink, voicelink
from devices import config as device_config
from api.main import app
from tests.conftest import AUTH


def test_fresh_package_waits_for_voice_choice_but_existing_installs_keep_it(
    monkeypatch,
):
    monkeypatch.setattr(voicelink.settings, "INSTALL_KIND", "package")
    monkeypatch.setattr(device_config, "load_user_config", lambda: {})
    assert voicelink.enabled() is False

    monkeypatch.setattr(device_config, "load_user_config",
                        lambda: {"music": {"driver": "AppleMusic"}})
    assert voicelink.enabled() is True

    monkeypatch.setattr(device_config, "load_user_config",
                        lambda: {"voice": {"enabled": False}})
    assert voicelink.enabled() is False


def test_voice_preparation_proves_model_and_writes_private_marker(
    monkeypatch, tmp_path,
):
    marker = tmp_path / ".avctl" / "voice-ready.json"
    monkeypatch.setattr(voicelink, "_ready_file", lambda: marker)
    monkeypatch.setattr(voicelink.sys, "platform", "darwin")
    monkeypatch.setattr(voicelink.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(voicelink.importlib.util, "find_spec",
                        lambda _name: object())
    monkeypatch.setattr(voicelink, "recognition_prompt", lambda: "prompt")
    monkeypatch.setattr(voicelink, "_infer", lambda _source, _prompt: {})
    numpy = types.ModuleType("numpy")
    numpy.float32 = float
    numpy.zeros = lambda count, dtype=None: [0.0] * count
    monkeypatch.setitem(voicelink.sys.modules, "numpy", numpy)

    result = voicelink.prepare()

    assert result["ready"] is True
    assert marker.stat().st_mode & 0o777 == 0o600
    assert voicelink.MODEL in marker.read_text()


def test_voice_prompt_prioritizes_personal_artist_spellings(monkeypatch):
    monkeypatch.setattr(musiclink, "recent_songs", lambda force=False: [
        "old schema row",
        {"artist": "Hikaru Utada", "albumArtist": "Hikaru Utada",
         "name": "Automatic", "album": "First Love",
         "plays": 80, "favorited": True},
        {"artist": "周杰倫", "albumArtist": "周杰倫", "plays": 20},
        {"artist": "One Play", "albumArtist": "Various Artists", "plays": 1},
    ])

    prompt = voicelink.recognition_prompt()

    assert "Hikaru Utada" in prompt
    assert "Automatic" in prompt
    assert "周杰倫" in prompt
    assert "别鸡巴播了" in prompt
    assert prompt.endswith("打开，关闭。")
    assert prompt.index("One Play") < prompt.index("Hikaru Utada")
    assert len(prompt) <= 700


def test_voice_prompt_is_reused_while_library_cache_is_unchanged(monkeypatch):
    songs = [{"artist": "Hikaru Utada", "name": "Automatic", "plays": 80}]
    refreshes = []
    monkeypatch.setattr(
        musiclink, "recent_songs",
        lambda force=False: refreshes.append(force) or [dict(songs[0])])
    monkeypatch.setattr(voicelink, "_PROMPT_CACHE", None)

    first = voicelink.recognition_prompt()
    second = voicelink.recognition_prompt()

    assert second is first
    assert "Automatic" in second
    assert refreshes == [True, True]


def test_voice_prompt_keeps_last_good_vocabulary_during_library_outage(
        monkeypatch):
    songs = [{"artist": "Hikaru Utada", "plays": 80}]
    monkeypatch.setattr(musiclink, "recent_songs", lambda force=False: songs)
    monkeypatch.setattr(voicelink, "_PROMPT_CACHE", None)
    prompt = voicelink.recognition_prompt()
    monkeypatch.setattr(
        musiclink, "recent_songs",
        lambda force=False: (_ for _ in ()).throw(RuntimeError("Music unavailable")),
    )

    assert voicelink.recognition_prompt() is prompt


def test_voice_prompt_refreshes_new_manual_library_addition(monkeypatch):
    old = {"artist": "Old Artist", "name": "Old Song", "plays": 90,
           "added": 1_700_000_000_000}
    new = {"artist": "Newly Added Artist", "name": "Zero Play New Song",
           "plays": 0, "added": 1_800_000_000_000}
    snapshots = iter(([old], [old, new]))
    refreshes = []
    monkeypatch.setattr(
        musiclink, "recent_songs",
        lambda force=False: refreshes.append(force) or next(snapshots))
    monkeypatch.setattr(voicelink, "_PROMPT_CACHE", None)

    first = voicelink.recognition_prompt()
    second = voicelink.recognition_prompt()

    assert "Zero Play New Song" not in first
    assert "Zero Play New Song" in second
    assert "Newly Added Artist" in second
    assert refreshes == [True, True]


def test_legacy_voice_decode_is_bounded_before_mlx(monkeypatch):
    calls = []
    decoded = _pcm_wav()[44:]

    class FakeWhisper:
        @staticmethod
        def transcribe(source, **options):
            calls.append((source, options))
            return {"text": "  播放我喜欢的 Hikaru Utada 的歌。  ",
                    "segments": [{
                        "text": "播放我喜欢的 Hikaru Utada 的歌。",
                        "avg_logprob": -0.2, "no_speech_prob": 0.1,
                        "compression_ratio": 1.1,
                    }]}

    monkeypatch.setattr(voicelink, "_backend", lambda: FakeWhisper)
    monkeypatch.setattr(voicelink, "_prepare_ffmpeg", lambda: None)
    monkeypatch.setattr(voicelink, "recognition_prompt", lambda: "library names")
    monkeypatch.setattr(voicelink.shutil, "which", lambda _name: "/ffmpeg")
    ffmpeg = []

    def fake_run(command, **kwargs):
        path = Path(command[command.index("-i") + 1])
        ffmpeg.append((command, kwargs, path.read_bytes(), path))
        return type("Completed", (), {"returncode": 0, "stdout": decoded})()

    monkeypatch.setattr(voicelink.subprocess, "run", fake_run)

    transcript = voicelink.transcribe(b"fake-m4a", "audio/mp4; charset=binary")

    assert transcript == "播放我喜欢的 Hikaru Utada 的歌。"
    source, options = calls[0]
    assert memoryview(source).format == "f"
    assert memoryview(source).itemsize == 4
    assert options == {
        "path_or_hf_repo": "mlx-community/whisper-large-v3-turbo",
        "initial_prompt": "library names",
        "temperature": 0.0,
        "condition_on_previous_text": False,
        "without_timestamps": True,
        "sample_len": 128,
    }
    command, kwargs, uploaded, path = ffmpeg[0]
    assert command[0] == "/ffmpeg"
    assert command[command.index("-t") + 1] == "36"
    assert uploaded == b"fake-m4a"
    assert kwargs["timeout"] == 15
    assert not path.exists()


def _pcm_wav(samples: list[int] | None = None) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(16_000)
        values = samples or ([0, 1000, -1000] * 1_400)
        target.writeframes(struct.pack("<" + "h" * len(values), *values))
    return output.getvalue()


def test_voice_pcm_wav_bypasses_ffmpeg_and_tempfile(monkeypatch):
    calls = []

    class FakeWhisper:
        @staticmethod
        def transcribe(source, **options):
            calls.append((source, options))
            return {"text": "下一首", "segments": [{
                "text": "下一首", "avg_logprob": -0.2,
                "no_speech_prob": 0.1, "compression_ratio": 1.1,
            }]}

    monkeypatch.setattr(voicelink, "_backend", lambda: FakeWhisper)
    monkeypatch.setattr(voicelink, "recognition_prompt", lambda: "prompt")
    monkeypatch.setattr(
        voicelink, "_prepare_ffmpeg",
        lambda: (_ for _ in ()).throw(AssertionError("WAV must bypass ffmpeg")),
    )

    assert voicelink.transcribe(_pcm_wav(), "audio/wav") == "下一首"
    assert memoryview(calls[0][0]).format == "f"
    assert memoryview(calls[0][0]).itemsize == 4
    assert list(calls[0][0][:3]) == pytest.approx(
        [0, 1000 / 32768, -1000 / 32768])


def test_voice_rejects_malformed_wav_before_inference(monkeypatch):
    monkeypatch.setattr(
        voicelink, "_backend",
        lambda: (_ for _ in ()).throw(AssertionError("must not infer")),
    )

    with pytest.raises(voicelink.VoiceError, match="valid WAV"):
        voicelink.transcribe(b"not-wave", "audio/wav")


def test_voice_rejects_silent_pcm_before_inference(monkeypatch):
    monkeypatch.setattr(
        voicelink, "_backend",
        lambda: (_ for _ in ()).throw(AssertionError("must not infer")),
    )

    with pytest.raises(voicelink.VoiceError, match="could not hear"):
        voicelink.transcribe(_pcm_wav([0] * 8_000), "audio/wav")


def test_voice_rejects_single_impulse_as_non_speech(monkeypatch):
    monkeypatch.setattr(
        voicelink, "_backend",
        lambda: (_ for _ in ()).throw(AssertionError("must not infer")),
    )
    samples = [0] * 8_000
    samples[4_000] = 20_000

    with pytest.raises(voicelink.VoiceError, match="could not hear"):
        voicelink.transcribe(_pcm_wav(samples), "audio/wav")


def test_voice_does_not_trust_forged_wav_duration(monkeypatch):
    forged = bytearray(_pcm_wav([1000, -1000]))
    forged[40:44] = struct.pack("<I", 8_000)  # claims 4,000 samples
    monkeypatch.setattr(
        voicelink, "_backend",
        lambda: (_ for _ in ()).throw(AssertionError("must not infer")),
    )

    with pytest.raises(voicelink.VoiceError, match="too short"):
        voicelink.transcribe(bytes(forged), "audio/wav")


def test_voice_rejects_unexpected_backend_result(monkeypatch):
    monkeypatch.setattr(
        voicelink, "_backend",
        lambda: type("Backend", (), {"transcribe": staticmethod(lambda *_a, **_k: [])}),
    )

    with pytest.raises(voicelink.VoiceError, match="invalid result"):
        voicelink.transcribe(_pcm_wav(), "audio/wav")


def test_voice_rejects_text_without_segment_confidence():
    with pytest.raises(voicelink.VoiceError, match="invalid result"):
        voicelink._transcript_from_result({"text": "turn the TV on"})


def test_voice_rejects_runaway_text_instead_of_truncating_command():
    text = "play " + "a" * 2_100 + " don't"
    result = {"text": text, "segments": [{
        "text": text, "avg_logprob": -0.2,
        "no_speech_prob": 0.1, "compression_ratio": 1.1,
    }]}

    with pytest.raises(voicelink.VoiceError, match="implausibly long"):
        voicelink._transcript_from_result(result)


def test_voice_drops_low_confidence_hallucination_segments(monkeypatch):
    result = {
        "text": "下一首 Thanks for watching",
        "segments": [
            {"text": "下一首", "avg_logprob": -0.3,
             "no_speech_prob": 0.1, "compression_ratio": 1.1},
            {"text": "Thanks for watching", "avg_logprob": -3.0,
             "no_speech_prob": 0.96, "compression_ratio": 1.2},
        ],
    }

    assert voicelink._transcript_from_result(result) == "下一首"


@pytest.mark.parametrize("weak_text,remaining", [
    ("don't", "turn the TV on"),
    ("不要", "打开电视"),
])
def test_voice_never_drops_uncertain_negation_from_action(
        weak_text, remaining):
    result = {
        "text": f"{weak_text} {remaining}",
        "segments": [
            {"text": weak_text, "avg_logprob": -2.4,
             "no_speech_prob": 0.2, "compression_ratio": 1.0},
            {"text": remaining, "avg_logprob": -0.2,
             "no_speech_prob": 0.1, "compression_ratio": 1.0},
        ],
    }

    with pytest.raises(voicelink.VoiceError, match="whole command"):
        voicelink._transcript_from_result(result)


def test_voice_rejects_unknown_low_confidence_tail_instead_of_truncating():
    result = {
        "text": "set the amp to 40 garbled qualifier",
        "segments": [
            {"text": "set the amp to 40", "avg_logprob": -0.2,
             "no_speech_prob": 0.1, "compression_ratio": 1.0},
            {"text": "garbled qualifier", "avg_logprob": -2.5,
             "no_speech_prob": 0.5, "compression_ratio": 1.0},
        ],
    }

    with pytest.raises(voicelink.VoiceError, match="whole command"):
        voicelink._transcript_from_result(result)


@pytest.mark.parametrize("result", [
    {"text": "下一首", "segments": [{
        "text": "下一首", "avg_logprob": -3.0,
        "no_speech_prob": 0.95, "compression_ratio": 1.1,
    }]},
    {"text": "[Music]", "segments": [{
        "text": "[Music]", "avg_logprob": -0.2,
        "no_speech_prob": 0.1, "compression_ratio": 1.1,
    }]},
])
def test_voice_never_promotes_noise_markers_to_commands(result):
    with pytest.raises(voicelink.VoiceError, match="could not hear"):
        voicelink._transcript_from_result(result)


def test_voice_finds_homebrew_ffmpeg_when_service_path_is_minimal(
        monkeypatch, tmp_path):
    ffmpeg = tmp_path / "bin" / "ffmpeg"
    ffmpeg.parent.mkdir()
    ffmpeg.write_text("#!/bin/sh\n")
    ffmpeg.chmod(0o755)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.delenv("AVCTL_FFMPEG", raising=False)
    monkeypatch.setattr(voicelink.shutil, "which", lambda _name: None)
    monkeypatch.setattr(voicelink, "_FFMPEG_LOCATIONS", (ffmpeg,))

    voicelink._prepare_ffmpeg()

    assert str(ffmpeg.parent) == voicelink.os.environ["PATH"].split(
        voicelink.os.pathsep)[0]


def test_legacy_voice_decode_timeout_is_bounded(monkeypatch):
    monkeypatch.setattr(voicelink, "_prepare_ffmpeg", lambda: None)
    monkeypatch.setattr(voicelink.shutil, "which", lambda _name: "/ffmpeg")
    monkeypatch.setattr(
        voicelink.subprocess, "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            voicelink.subprocess.TimeoutExpired("ffmpeg", 15)),
    )

    with pytest.raises(voicelink.VoiceError, match="too long to decode"):
        voicelink._legacy_waveform(b"compressed", ".m4a")


def test_legacy_voice_rejects_overlong_decode_instead_of_truncating(
        monkeypatch):
    monkeypatch.setattr(voicelink, "_prepare_ffmpeg", lambda: None)
    monkeypatch.setattr(voicelink.shutil, "which", lambda _name: "/ffmpeg")
    decoded = b"\x01\x00" * (voicelink.MAX_AUDIO_SECONDS * 16_000 + 1)
    monkeypatch.setattr(
        voicelink.subprocess, "run",
        lambda *_args, **_kwargs: type(
            "Completed", (), {"returncode": 0, "stdout": decoded})(),
    )

    with pytest.raises(voicelink.VoiceError, match="too long"):
        voicelink._legacy_waveform(b"compressed", ".m4a")


def test_voice_admission_slot_is_released_after_decode_failure(monkeypatch):
    class Slot:
        releases = 0

        @staticmethod
        def acquire(blocking=False):
            assert blocking is False
            return True

        @classmethod
        def release(cls):
            cls.releases += 1

    monkeypatch.setattr(voicelink, "_TRANSCRIBE_SLOTS", Slot)
    monkeypatch.setattr(
        voicelink, "_legacy_waveform",
        lambda _audio, _suffix: (_ for _ in ()).throw(
            voicelink.VoiceError("decode failed")),
    )

    with pytest.raises(voicelink.VoiceError, match="decode failed"):
        voicelink.transcribe(b"compressed", "audio/mp4")

    assert Slot.releases == 1


def test_voice_inference_lock_wait_is_bounded(monkeypatch):
    class StuckLock:
        @staticmethod
        def acquire(timeout):
            assert timeout == voicelink.INFERENCE_WAIT_SECONDS == 20
            return False

        @staticmethod
        def release():
            raise AssertionError("an unacquired lock must not be released")

    monkeypatch.setattr(voicelink, "_TRANSCRIBE_LOCK", StuckLock)
    monkeypatch.setattr(
        voicelink, "_backend",
        lambda: (_ for _ in ()).throw(AssertionError("must not infer")),
    )

    with pytest.raises(voicelink.VoiceError, match="still busy"):
        voicelink._infer([0.0], "prompt")


def test_voice_rejects_long_recognition_prompt_echo(monkeypatch):
    echoed = ("Common avctl commands: play, pause, stop playing, next track, "
              "previous track")

    class EchoingWhisper:
        @staticmethod
        def transcribe(_source, **_options):
            return {"segments": [{
                "text": echoed, "avg_logprob": -0.1,
                "no_speech_prob": 0.01, "compression_ratio": 1.1,
            }]}

    monkeypatch.setattr(voicelink, "_backend", lambda: EchoingWhisper)
    monkeypatch.setattr(
        voicelink, "recognition_prompt", lambda: echoed + ". personal names")

    with pytest.raises(voicelink.VoiceError, match="could not hear"):
        voicelink.transcribe(_pcm_wav(), "audio/wav")


def test_voice_prompt_echo_guard_keeps_short_real_command():
    assert voicelink._is_prompt_echo("下一首", "控制命令：播放，下一首，上一首") is False


def test_voice_api_is_authenticated_and_feeds_transcript_to_agent(monkeypatch):
    calls = []
    monkeypatch.setattr(
        voicelink, "transcribe",
        lambda audio, content_type: calls.append(
            ("voice", audio, content_type)) or "播放青花瓷",
    )
    monkeypatch.setattr(
        agentlink, "ask",
        lambda message, session, caller, **_kwargs: calls.append(
            ("agent", message, session, caller)) or
            {"message": "playing 青花瓷 — Jay Chou", "acted": True},
    )

    with TestClient(app) as client:
        denied = client.post(
            "/api/agent/voice?session=session_123",
            content=b"recording", headers={"Content-Type": "audio/mp4"})
        answer = client.post(
            "/api/agent/voice?session=session_123", content=b"recording",
            headers={**AUTH, "Content-Type": "audio/mp4"})

    assert denied.status_code == 401
    assert answer.json() == {
        "status": "ok",
        "transcript": "播放青花瓷",
        "message": "playing 青花瓷 — Jay Chou",
        "acted": True,
    }
    assert "transcribe;dur=" in answer.headers["server-timing"]
    assert calls == [
        ("voice", b"recording", "audio/mp4"),
        ("agent", "播放青花瓷", "session_123", "token"),
    ]


def test_voice_api_rejects_unknown_format_before_transcription(monkeypatch):
    monkeypatch.setattr(
        voicelink, "transcribe",
        lambda *_: (_ for _ in ()).throw(AssertionError("must not transcribe")),
    )

    with TestClient(app) as client:
        answer = client.post(
            "/api/agent/voice?session=session_123", content=b"not audio",
            headers={**AUTH, "Content-Type": "application/json"})

    assert answer.status_code == 415
    assert answer.json()["detail"] == "unsupported audio format"


def test_voice_api_preserves_transcript_when_agent_fails(monkeypatch):
    monkeypatch.setattr(
        voicelink, "transcribe", lambda _audio, _content_type: "下一首",
    )
    monkeypatch.setattr(
        agentlink, "ask",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            agentlink.AgentError("offline")),
    )

    with TestClient(app) as client:
        answer = client.post(
            "/api/agent/voice?session=session_123", content=b"recording",
            headers={**AUTH, "Content-Type": "audio/mp4"})

    assert answer.status_code == 502
    assert answer.json() == {"detail": "offline", "transcript": "下一首"}
    assert "transcribe;dur=" in answer.headers["server-timing"]


def test_voice_api_preserves_transcript_when_conversation_is_busy(monkeypatch):
    monkeypatch.setattr(
        voicelink, "transcribe", lambda _audio, _content_type: "下一首",
    )

    async def busy(*_args, **_kwargs):
        raise main._AgentSessionBusy

    monkeypatch.setattr(main, "_ask_while_connected", busy)

    with TestClient(app) as client:
        answer = client.post(
            "/api/agent/voice?session=session_123", content=b"recording",
            headers={**AUTH, "Content-Type": "audio/mp4"})

    assert answer.status_code == 409
    assert answer.json() == {
        "detail": "this conversation is already handling another request",
        "transcript": "下一首",
    }
    assert "transcribe;dur=" in answer.headers["server-timing"]


def test_voice_api_preserves_transcript_on_unexpected_agent_failure(monkeypatch):
    monkeypatch.setattr(
        voicelink, "transcribe", lambda _audio, _content_type: "下一首",
    )
    monkeypatch.setattr(
        agentlink, "ask",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AttributeError("private device detail")),
    )

    with TestClient(app) as client:
        answer = client.post(
            "/api/agent/voice?session=session_123", content=b"recording",
            headers={**AUTH, "Content-Type": "audio/mp4"})

    assert answer.status_code == 502
    assert answer.json() == {
        "detail": ("agent failed unexpectedly (AttributeError); action status "
                   "is unknown, so check before retrying"),
        "transcript": "下一首",
    }
    assert "private device detail" not in answer.text


def test_voice_disconnect_after_transcription_never_reaches_agent(monkeypatch):
    monkeypatch.setattr(
        voicelink, "transcribe", lambda _audio, _content_type: "turn it off",
    )
    monkeypatch.setattr(
        agentlink, "ask",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("must not act")),
    )

    async def disconnected(_request):
        return True

    monkeypatch.setattr(Request, "is_disconnected", disconnected)

    with TestClient(app) as client:
        answer = client.post(
            "/api/agent/voice?session=session_123", content=b"recording",
            headers={**AUTH, "Content-Type": "audio/mp4"})

    assert answer.status_code == 499
    assert answer.json() == {
        "detail": "voice request was cancelled before action",
        "transcript": "turn it off",
    }


def test_broken_disconnect_channel_fails_closed_before_agent_action(monkeypatch):
    class BrokenRequest:
        @staticmethod
        async def is_disconnected():
            return False

        @staticmethod
        async def receive():
            raise RuntimeError("ASGI channel closed")

    def wait_for_disconnect(_message, _session, _caller, *, cancelled):
        while not cancelled():
            threading.Event().wait(0.001)
        raise agentlink.AgentError("request was cancelled before action")

    monkeypatch.setattr(agentlink, "ask", wait_for_disconnect)

    with pytest.raises(main._AgentClientGone):
        asyncio.run(main._ask_while_connected(
            BrokenRequest(), "next", "session_123", "caller"))


def test_voice_api_rejects_bad_session_before_transcription(monkeypatch):
    monkeypatch.setattr(
        voicelink, "transcribe",
        lambda *_: (_ for _ in ()).throw(AssertionError("must not transcribe")),
    )

    with TestClient(app) as client:
        answer = client.post(
            "/api/agent/voice?session=bad", content=b"recording",
            headers={**AUTH, "Content-Type": "audio/mp4"})

    assert answer.status_code == 400
    assert answer.json()["detail"] == "invalid session id"


def test_voice_api_stops_stream_at_size_limit(monkeypatch):
    monkeypatch.setattr(voicelink, "MAX_AUDIO_BYTES", 4)
    monkeypatch.setattr(
        voicelink, "transcribe",
        lambda *_: (_ for _ in ()).throw(AssertionError("must not transcribe")),
    )

    with TestClient(app) as client:
        answer = client.post(
            "/api/agent/voice?session=session_123", content=b"12345",
            headers={**AUTH, "Content-Type": "audio/mp4"})

    assert answer.status_code == 413
    assert answer.json()["detail"] == "recording is too large"


def test_ask_markup_and_script_expose_native_hold_to_talk():
    from api import views

    root = Path(__file__).resolve().parents[1]
    html = views.remote(type("Identity", (), {
        "method": "token", "display": None, "who": "token"})())
    javascript = (root / "api/ui/app.js").read_text()
    stylesheet = (root / "api/ui/app.css").read_text()

    assert "id='agent-mode'" in html
    assert "id='agent-hold'" in html
    assert "Hold to talk" in html
    assert "event: 'voice-start', session: agentSession" in javascript
    assert "event: shouldCancel ? 'voice-cancel' : 'voice-stop'" in javascript
    assert "voiceStartY - event.clientY > 64" in javascript
    assert "lostpointercapture" in javascript
    assert "agentHold.disabled = true" not in javascript
    assert "Voice input lost its native connection." in javascript
    assert "The action status is unknown, so check before retrying." in javascript
    assert "agentInput.value = transcript" in javascript
    assert "agentInput.dispatchEvent(new Event('input'))" in javascript
    assert "agentMessage(message, 'agent', false)" in javascript
    assert ".agent-compose.voice-cancelling .agent-hold" in stylesheet
    swift = (root / "app/avctl/VoiceCapture.swift").read_text()
    assert "uploadTask?.cancel()" in swift
    assert "generation == owner.uploadGeneration" in swift
    assert "same-generation cancellation" in swift
    assert "An action may already have started; check before retrying." in swift
    assert "The action status is unknown, so check before retrying." in swift
    assert "Voice input is already busy." in swift
    assert "RunLoop.main.add(timer, forMode: .common)" in swift
    assert 'request.setValue("audio/wav"' in swift
    assert 'transcript: payload["transcript"] as? String' in swift
    panel_swift = (root / "app/avctl/PanelView.swift").read_text()
    assert "context.coordinator.setAppActive(active)" in panel_swift
    assert 'sendVoiceEvent(["state": "cancelled"])' in panel_swift
