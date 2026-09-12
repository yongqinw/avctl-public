"""Local speech recognition for the Ask panel.

The phone records; the Mac mini transcribes.  Keeping Whisper here means the
iOS app carries no model, model updates do not require reinstalling the app,
and the transcript enters the exact same agent/tool boundary as typed text.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import wave
from array import array
from io import BytesIO
from collections import defaultdict
from pathlib import Path
from typing import Any

from devices import config as device_config

from . import musiclink, settings


class VoiceError(RuntimeError):
    """A safe, user-visible local transcription failure."""


MODEL = os.environ.get(
    "AVCTL_WHISPER_MODEL", "mlx-community/whisper-large-v3-turbo")
MAX_AUDIO_BYTES = 8 * 1024 * 1024
MAX_AUDIO_SECONDS = 35
MIN_AUDIO_SECONDS = 0.25
INFERENCE_WAIT_SECONDS = 20

_CONTENT_SUFFIX = {
    "audio/aiff": ".aiff",
    "audio/mp4": ".m4a",
    "audio/m4a": ".m4a",
    "audio/mpeg": ".mp3",
    "audio/wav": ".wav",
    "audio/webm": ".webm",
    "audio/x-m4a": ".m4a",
    "audio/x-wav": ".wav",
}
_TRANSCRIBE_LOCK = threading.Lock()
_PROMPT_LOCK = threading.Lock()
_PROMPT_CACHE: tuple[str, str] | None = None
# Bound admission as well as inference. One phone may wait behind the active
# transcription; a third is rejected immediately instead of consuming an
# AnyIO worker for minutes while Metal is already saturated.
_TRANSCRIBE_SLOTS = threading.BoundedSemaphore(2)
_FFMPEG_LOCATIONS = (
    Path("/opt/homebrew/bin/ffmpeg"),
    Path("/usr/local/bin/ffmpeg"),
)
_CONTROL_VOCABULARY = (
    "Common avctl commands: play, pause, stop playing, next track, previous "
    "track, louder, quieter, mute. 控制命令：播放，暂停，停止播放，别播了，"
    "别鸡巴播了，下一首，上一首，大声点，小声点，音量，静音，打开，关闭。"
)


def _ready_file() -> Path:
    return settings.TOKEN_FILE.parent / "voice-ready.json"


def enabled() -> bool:
    """Whether this Core's owner chose voice Ask.

    Existing configured installations predate the switch and keep their
    working voice behavior. A brand-new packaged Core stays quiet until the
    setup wizard explicitly enables and prepares voice.
    """
    configured = device_config.load_user_config()
    voice = configured.get("voice") if isinstance(configured, dict) else None
    if isinstance(voice, dict) and "enabled" in voice:
        return bool(voice["enabled"])
    # Source installs and pre-wizard configurations historically had voice
    # enabled. Only a genuinely fresh package waits for the owner's choice.
    return bool(configured) or settings.INSTALL_KIND != "package"


def status() -> dict[str, Any]:
    """Return non-secret installer facts about the local voice runtime."""
    supported_host = sys.platform == "darwin" and platform.machine() == "arm64"
    runtime = importlib.util.find_spec("mlx_whisper") is not None
    prepared = False
    try:
        marker = json.loads(_ready_file().read_text(encoding="utf-8"))
        prepared = runtime and marker.get("model") == MODEL
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        pass
    return {
        "supported": supported_host,
        "runtime": runtime,
        "prepared": prepared,
        "ready": supported_host and runtime and prepared,
        "enabled": enabled(),
        "model": MODEL,
        "detail": (
            "Transcription model is ready"
            if supported_host and runtime and prepared else
            "Bundled transcription runtime needs model preparation"
            if supported_host and runtime else
            "This package is missing its MLX transcription runtime"
            if supported_host else
            "Local voice transcription requires an Apple-silicon Mac"
        ),
    }


def prepare() -> dict[str, Any]:
    """Download/load the configured model and prove one inference completes."""
    facts = status()
    if not facts["supported"]:
        raise VoiceError("local voice transcription requires an Apple-silicon Mac")
    if not facts["runtime"]:
        raise VoiceError("this package is missing its MLX transcription runtime")
    try:
        import numpy as np
        prompt = recognition_prompt()
        _infer(np.zeros(1_600, dtype=np.float32), prompt)
        destination = _ready_file()
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        destination.parent.chmod(0o700)
        temporary = destination.with_name(destination.name + ".tmp")
        temporary.write_text(json.dumps({"model": MODEL}) + "\n", encoding="utf-8")
        temporary.chmod(0o600)
        os.replace(temporary, destination)
    except VoiceError:
        raise
    except Exception as exc:
        raise VoiceError(
            f"local transcription preparation failed ({type(exc).__name__})"
        ) from None
    return status()


def supported(content_type: str | None) -> bool:
    """Whether an upload has a container ffmpeg/Whisper can identify."""
    media_type = str(content_type or "").partition(";")[0].strip().lower()
    return media_type in _CONTENT_SUFFIX


def _suffix(content_type: str) -> str:
    media_type = content_type.partition(";")[0].strip().lower()
    try:
        return _CONTENT_SUFFIX[media_type]
    except KeyError:
        raise VoiceError("unsupported audio format") from None


def recognition_prompt() -> str:
    """A compact personal vocabulary, ranked to fit Whisper's prompt window.

    Artist spelling matters most for voice commands.  Frequently played and
    favorited names receive priority; album artists also accrue the plays of
    their tracks.  This is recognition context only, never an instruction.
    """
    global _PROMPT_CACHE
    try:
        # Voice spelling must see a manual Music.app addition immediately,
        # just like the Ask system prompt does. The digest below still keeps
        # the generated prompt object stable when the refreshed facts match.
        songs = musiclink.recent_songs(force=True)
    except (OSError, RuntimeError, TypeError, ValueError):
        with _PROMPT_LOCK:
            if _PROMPT_CACHE is not None:
                return _PROMPT_CACHE[1]
        songs = []
    relevant = [{key: row.get(key) for key in
                 ("name", "artist", "album", "albumArtist", "plays",
                  "favorited", "added")}
                for row in songs if isinstance(row, dict)]
    digest = hashlib.sha256(repr(relevant).encode("utf-8")).hexdigest()
    with _PROMPT_LOCK:
        if _PROMPT_CACHE is not None and _PROMPT_CACHE[0] == digest:
            return _PROMPT_CACHE[1]

    scores: dict[str, int] = defaultdict(int)
    display: dict[str, str] = {}
    added_values = []
    for song in songs:
        if isinstance(song, dict):
            try:
                added_values.append(float(song.get("added") or 0))
            except (TypeError, ValueError):
                pass
    newest_added = max(added_values, default=0)
    for song in songs:
        if not isinstance(song, dict):
            continue
        try:
            plays = int(song.get("plays") or 0)
        except (TypeError, ValueError):
            plays = 0
        weight = 1 + min(max(plays, 0), 100)
        if song.get("favorited"):
            weight += 30
        try:
            added = float(song.get("added") or 0)
        except (TypeError, ValueError):
            added = 0
        # Freshly imported names often have zero plays, exactly when speech
        # recognition needs their spelling most. Rank the newest two weeks of
        # the current library snapshot above old high-play metadata.
        if newest_added and added >= newest_added - 14 * 86400 * 1000:
            weight += 80
        for field in ("artist", "albumArtist"):
            name = re.sub(r"\s+", " ", str(song.get(field) or "")).strip()
            if not name:
                continue
            key = name.casefold()
            display.setdefault(key, name)
            scores[key] += weight
        # A transcript must spell the requested recording before the agent's
        # rich library snapshot can resolve it. Personal high-signal titles
        # share the same small prompt budget with artist names.
        for field, multiplier in (("name", 2), ("album", 1)):
            name = re.sub(r"\s+", " ", str(song.get(field) or "")).strip()
            if not name:
                continue
            key = name.casefold()
            display.setdefault(key, name)
            scores[key] += weight * multiplier

    ranked = sorted(scores, key=lambda key: (-scores[key], display[key].casefold()))
    prefix = ("avctl equipment and music vocabulary: Apple Music, Mac mini, "
              "McIntosh, Topping D900")
    chosen: list[str] = []
    length = len(prefix) + len(_CONTROL_VOCABULARY) + 2
    for key in ranked:
        # Whisper reserves only a small decoder window for initial_prompt.
        # A short high-signal list beats silently truncating a library dump.
        added = 2 + len(display[key])
        if length + added > 700:
            break
        chosen.append(display[key])
        length += added
    # Whisper retains the tail if a tokenized prompt is still too long, so
    # put the highest-ranked personal spellings last where they survive.
    artists = ", " + ", ".join(reversed(chosen)) if chosen else ""
    # Command words sit at the very tail: very short utterances carry too
    # little evidence, and Whisper otherwise turns colloquial Mandarin into
    # plausible-looking homophones or unspaced pinyin.
    prompt = prefix + artists + ". " + _CONTROL_VOCABULARY
    with _PROMPT_LOCK:
        _PROMPT_CACHE = (digest, prompt)
    return prompt


def _backend() -> Any:
    try:
        import mlx_whisper
    except (ImportError, OSError) as exc:
        raise VoiceError(
            "local transcription runtime could not load "
            f"({type(exc).__name__}: {exc})") from exc
    return mlx_whisper


def _mlx_options(prompt: str) -> dict[str, Any]:
    """Shared low-latency decoding settings for warmup and real commands."""
    return {
        "path_or_hf_repo": MODEL,
        "initial_prompt": prompt,
        "temperature": 0.0,
        "condition_on_previous_text": False,
        # Ask needs text, never segment timestamps. A bounded 30-second voice
        # command fits comfortably in 128 tokens; this also stops a low-
        # confidence hallucination from filling Whisper's 224-token default.
        "without_timestamps": True,
        "sample_len": 128,
    }


def _infer(source: Any, prompt: str) -> Any:
    """Serialize Metal inference without letting one stuck call queue forever."""
    if not _TRANSCRIBE_LOCK.acquire(timeout=INFERENCE_WAIT_SECONDS):
        raise VoiceError(
            "local transcription is still busy; try again in a moment")
    try:
        return _backend().transcribe(source, **_mlx_options(prompt))
    finally:
        _TRANSCRIBE_LOCK.release()


def _waveform_from_pcm(frames: bytes) -> Any:
    """Validate little-endian 16-bit mono samples and convert for MLX."""
    if not frames:
        raise VoiceError("the recording was empty")
    try:
        import numpy as np
        raw = np.frombuffer(frames, dtype="<i2")
        if raw.size < MIN_AUDIO_SECONDS * 16_000:
            raise VoiceError("the recording was too short")
        # Cast before abs: abs(int16(-32768)) overflows back to -32768.
        magnitude = np.abs(raw.astype(np.int32))
        # Require both a real peak and sustained energy. A tap/click can have
        # one large sample but is not speech and should never be promoted into
        # a hallucinated hardware command.
        if int(magnitude.max()) < 100 or float(magnitude.mean()) < 20:
            raise VoiceError("I could not hear a command")
        # MLX installs NumPy on the mini. Keep silence detection and float
        # conversion vectorized so a 30-second hold does not scan 480k
        # samples several times in Python before inference even begins.
        return raw.astype(np.float32) / 32768.0
    except (ImportError, OSError):
        # Linux CI intentionally does not install the macOS-only MLX stack.
        # mlx.core also accepts this equivalent Python buffer-array fallback.
        samples = array("h")
        try:
            samples.frombytes(frames)
        except ValueError as exc:
            raise VoiceError("the recording has invalid PCM data") from exc
        if sys.byteorder != "little":
            samples.byteswap()
        if len(samples) < MIN_AUDIO_SECONDS * 16_000:
            raise VoiceError("the recording was too short")
        peak = max(abs(sample) for sample in samples)
        mean_amplitude = sum(abs(sample) for sample in samples) / len(samples)
        if peak < 100 or mean_amplitude < 20:
            raise VoiceError("I could not hear a command")
        return array("f", (sample / 32768.0 for sample in samples))


def _pcm_waveform(audio: bytes) -> Any:
    """Decode the native app's 16 kHz mono PCM WAV without spawning ffmpeg."""
    try:
        with wave.open(BytesIO(audio), "rb") as source:
            if (source.getnchannels(), source.getsampwidth(),
                    source.getframerate(), source.getcomptype()) != (
                        1, 2, 16_000, "NONE"):
                raise VoiceError(
                    "voice WAV must be 16 kHz mono 16-bit PCM")
            frame_count = source.getnframes()
            if frame_count > MAX_AUDIO_SECONDS * 16_000:
                raise VoiceError("the recording is too long")
            if frame_count < MIN_AUDIO_SECONDS * 16_000:
                raise VoiceError("the recording was too short")
            frames = source.readframes(frame_count)
    except (EOFError, wave.Error) as exc:
        raise VoiceError("the recording is not a valid WAV file") from exc
    return _waveform_from_pcm(frames)


def _prepare_ffmpeg() -> None:
    """Make Homebrew ffmpeg visible inside launchd/deployer environments.

    mlx-whisper invokes ``ffmpeg`` by name.  The service intentionally does
    not run through an interactive shell, so Homebrew's bin directory is not
    necessarily on PATH even though the binary is installed on the mini.
    """
    if shutil.which("ffmpeg"):
        return
    configured = os.environ.get("AVCTL_FFMPEG")
    candidates = ((Path(configured),) if configured else ()) + _FFMPEG_LOCATIONS
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            current = os.environ.get("PATH", "")
            parent = str(candidate.parent)
            os.environ["PATH"] = parent + (os.pathsep + current if current else "")
            return
    raise VoiceError("ffmpeg is not installed on the Mac mini")


def _legacy_waveform(audio: bytes, suffix: str) -> Any:
    """Decode old compressed uploads into a bounded in-memory waveform.

    mlx-whisper's path loader decodes the complete input before applying clip
    timestamps. A small, low-bitrate file can therefore expand into minutes
    of PCM and monopolize Metal. Decode at most our recording limit here and
    pass MLX the already-bounded waveform instead.
    """
    _prepare_ffmpeg()
    executable = shutil.which("ffmpeg") or "ffmpeg"
    path: Path | None = None
    try:
        # MP4/M4A metadata can require seeking to the end and then back to the
        # media payload. Feeding those bytes through a non-seekable stdin pipe
        # succeeds for some short files but silently decodes zero samples for
        # normal recordings, so the legacy path still needs a temporary file.
        with tempfile.NamedTemporaryFile(
                prefix="avctl-voice-", suffix=suffix, delete=False) as handle:
            handle.write(audio)
            path = Path(handle.name)
        command = [
            executable, "-v", "error", "-nostdin", "-i", str(path),
            # Decode one second beyond the accepted limit. Truncating exactly
            # at the limit made an overlong command look valid and could drop
            # a trailing correction or negation before transcription.
            "-map", "0:a:0", "-t", str(MAX_AUDIO_SECONDS + 1),
            "-vn", "-sn", "-dn", "-f", "s16le",
            "-acodec", "pcm_s16le", "-ac", "1", "-ar", "16000",
            "pipe:1",
        ]
        completed = subprocess.run(
            command, capture_output=True, check=False, timeout=15)
        if completed.returncode != 0:
            raise VoiceError("the recording could not be decoded")
        if len(completed.stdout) > MAX_AUDIO_SECONDS * 16_000 * 2:
            raise VoiceError("the recording is too long")
        return _waveform_from_pcm(completed.stdout)
    except subprocess.TimeoutExpired:
        raise VoiceError("the recording took too long to decode") from None
    except OSError as exc:
        raise VoiceError(
            f"the recording could not be decoded ({type(exc).__name__})") from None
    finally:
        if path is not None:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass


def _transcript_from_result(result: Any) -> str:
    """Keep credible speech without ever deleting uncertain command words."""
    if not isinstance(result, dict):
        raise VoiceError("local transcription returned an invalid result")
    segments = result.get("segments")
    if segments is None:
        # The pinned MLX backend always returns segment confidence metadata.
        # A text-only result cannot prove that a weak negation was retained.
        raise VoiceError("local transcription returned an invalid result")
    else:
        if not isinstance(segments, list):
            raise VoiceError("local transcription returned an invalid result")
        credible: list[str] = []
        for segment in segments:
            if not isinstance(segment, dict):
                raise VoiceError(
                    "local transcription returned an invalid result")
            segment_text = str(segment.get("text") or "").strip()
            if not segment_text:
                continue
            try:
                average = float(segment["avg_logprob"])
                no_speech = float(segment["no_speech_prob"])
                compression = float(segment["compression_ratio"])
            except (KeyError, TypeError, ValueError):
                raise VoiceError(
                    "local transcription returned an invalid result") from None
            if not all(math.isfinite(value) for value in (
                    average, no_speech, compression)):
                raise VoiceError(
                    "local transcription returned an invalid result")
            credible_segment = (
                average >= -2.0 and no_speech <= 0.9 and compression <= 3.0)
            if credible_segment:
                credible.append(segment_text)
                continue
            # Whisper commonly appends these stock phrases to silence. They
            # carry no control meaning and are safe to discard. Any other
            # uncertain segment rejects the whole utterance: deleting a weak
            # "don't" or "不要" while retaining "turn the TV on" would invert
            # the caller's command before the intent gates ever see it.
            discardable_noise = bool(re.fullmatch(
                r"(?:thanks?|thank\s+you)(?:\s+for\s+watching)?[.!。！]?|"
                r"(?:please\s+)?subscribe[.!]?|"
                r"谢谢观看[。！]?|感謝觀看[。！]?|请订阅[。！]?|請訂閱[。！]?|"
                r"[\[(（【]\s*(?:music|applause|silence|noise|"
                r"音乐|音樂|掌声|掌聲|噪音)\s*[\])）】][.!。！]?",
                segment_text, re.IGNORECASE))
            if not discardable_noise:
                raise VoiceError("I could not hear the whole command clearly")
        text = " ".join(credible)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        raise VoiceError("I could not hear a command")
    if len(text) > 2000:
        # Never silently remove a trailing qualifier or negation. A real
        # 35-second command cannot approach this bound; a result that does is
        # decoder runaway, not useful speech.
        raise VoiceError("local transcription was implausibly long")
    if re.fullmatch(
            r"[\[(（【]\s*(?:music|applause|silence|noise|"
            r"音乐|音樂|掌声|掌聲|噪音)\s*[\])）】][.!。！]?",
            text, re.IGNORECASE):
        raise VoiceError("I could not hear a command")
    return text


def _is_prompt_echo(transcript: str, prompt: str) -> bool:
    """Reject long decoder copies of recognition context, never short commands."""
    if len(transcript) < 40:
        return False
    compact_transcript = re.sub(r"[^\w]+", "", transcript.casefold())
    compact_prompt = re.sub(r"[^\w]+", "", prompt.casefold())
    return (len(compact_transcript) >= 32
            and compact_transcript in compact_prompt)


def transcribe(audio: bytes, content_type: str) -> str:
    """Transcribe one short recording with the process-cached MLX model."""
    if not audio:
        raise VoiceError("the recording was empty")
    if len(audio) > MAX_AUDIO_BYTES:
        raise VoiceError("the recording is too large")

    suffix = _suffix(content_type)  # validate even when bypassing the route
    media_type = content_type.partition(";")[0].strip().lower()
    waveform = _pcm_waveform(audio) if media_type in {
        "audio/wav", "audio/x-wav",
    } else None
    if not _TRANSCRIBE_SLOTS.acquire(blocking=False):
        raise VoiceError("voice transcription is busy; try again in a moment")
    try:
        # Older app builds upload AAC. Keep them working, but bound decoded
        # duration before MLX allocates a spectrogram for the whole file.
        source = (waveform if waveform is not None
                  else _legacy_waveform(audio, suffix))
        prompt = recognition_prompt()
        # MLX keeps the loaded model in process. Serialize inference: two
        # phones wait briefly instead of competing for Metal memory.
        result = _infer(source, prompt)
    except VoiceError:
        raise
    except Exception as exc:
        raise VoiceError(
            f"local transcription failed ({type(exc).__name__})") from None
    finally:
        _TRANSCRIBE_SLOTS.release()

    transcript = _transcript_from_result(result)
    if _is_prompt_echo(transcript, prompt):
        raise VoiceError("I could not hear a command")
    return transcript


def warmup() -> None:
    """Load MLX weights and compile its short-audio path in the background."""
    if (not enabled() or sys.platform != "darwin" or platform.machine() != "arm64"
            or os.environ.get("AVCTL_WHISPER_WARMUP", "1") == "0"):
        return

    def load() -> None:
        try:
            import numpy as np
            prompt = recognition_prompt()
            _infer(np.zeros(1_600, dtype=np.float32), prompt)
        except Exception as exc:
            # Startup must not fail because an optional warmup failed. The
            # real request returns the safe, specific VoiceError later.
            print(f"  voice warmup    unavailable ({type(exc).__name__})",
                  flush=True)
        else:
            print("  voice warmup    ready", flush=True)

    threading.Thread(target=load, name="avctl-voice-warmup",
                     daemon=True).start()
