from __future__ import annotations

from api import amplink, musiclink
from devices import config as device_config
from devices.music import AppleMusic


class FakeAmp:
    def __init__(self, volume=68):
        self.volume = volume

    def query(self):
        return {"PWR": 1, "VOL": self.volume}

    def set_volume(self, level):
        self.volume = level
        return level


class FakeMusic:
    def __init__(self):
        self.volume = None

    def set_system_volume(self, level):
        self.volume = level


def safe_config():
    return {"amp": {"max_volume": 70}, "music": {"max_volume": 80}}


def test_amp_absolute_and_nudge_cannot_cross_70(monkeypatch):
    amp = FakeAmp()
    monkeypatch.setattr(device_config, "load_config", safe_config)
    monkeypatch.setattr(amplink, "_require", lambda: amp)

    assert amplink.vol_set({"level": 99})["message"] == \
        "volume 70 (limited to 70)"
    amp.volume = 70
    assert amplink.vol_up({})["message"] == "volume 70 (limited to 70)"
    amp.volume = 75
    assert amplink.vol_down({})["message"] == "volume 70 (limited to 70)"


def test_mini_absolute_volume_cannot_cross_80(monkeypatch):
    music = FakeMusic()
    monkeypatch.setattr(device_config, "load_config", safe_config)
    monkeypatch.setattr(musiclink, "_MUSIC", music)

    assert musiclink.vol_set({"level": 100})["message"] == \
        "mini volume 80% (limited to 80)"
    assert music.volume == 80


def test_apple_music_driver_is_a_second_volume_guard(monkeypatch):
    music = AppleMusic(max_system_volume=80)
    scripts = []
    monkeypatch.setattr(music, "_osascript",
                        lambda script, **kwargs: scripts.append(script) or "")

    music.set_system_volume(100)

    assert scripts == ["set volume output volume 80 output muted false"]
