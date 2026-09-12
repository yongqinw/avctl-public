"""Human-language Ask cases shared by the opt-in live prompt evaluator."""

from __future__ import annotations

from typing import Any


PROMPT_CASES: list[dict[str, Any]] = [
    {
        "name": "pause_direct",
        "prompt": "别播了。",
        "calls": [{"tool": "music_transport", "arguments": {"action": "pause"}}],
    },
    {
        "name": "angry_next",
        "prompt": "我操你妈赶紧下一首，别跟我废话。",
        "calls": [{"tool": "music_transport", "arguments": {"action": "next"}}],
    },
    {
        "name": "amp_volume",
        "prompt": "功放音量调到62。",
        "calls": [{"tool": "set_volume", "arguments": {"device": "amp", "level": 62}}],
    },
    {
        "name": "mini_mute",
        "prompt": "先把 Mac mini 静音。",
        "calls": [{"tool": "set_mute", "arguments": {"device": "mini", "muted": True}}],
    },
    {
        "name": "today_added",
        "prompt": "播放今天刚加进资料库的歌。",
        "calls": [{"tool": "play_library_added", "arguments": {
            "period": "today", "mode": "replace",
        }}],
    },
    {
        "name": "recently_added_week_shuffled",
        "prompt": "播放这周新加到资料库的歌，把它们随机播放。",
        "calls": [
            {"tool": "music_transport", "arguments": {"action": "shuffle_on"}},
            {"tool": "play_library_added", "arguments": {
                "period": "this_week", "mode": "replace",
            }},
        ],
    },
    {
        "name": "catalog_queue_no_import",
        "prompt": "Apple Music里把青花瓷放到Q后面听，别加进我的资料库。",
        "calls": [{"tool": "play_music_service", "arguments": {
            "query": "青花瓷", "artist": "周杰伦",
            "kind": "song", "mode": "append",
        }}],
        "forbidden_tools": ["add_added_to_playlist"],
    },
    {
        "name": "shuffle_on",
        "prompt": "把随机播放打开。",
        "calls": [{"tool": "music_transport", "arguments": {
            "action": "shuffle_on",
        }}],
    },
    {
        "name": "explore_without_playing",
        "prompt": "看看有什么我可能喜欢的音乐，先别播。",
        "calls": [{"tool": "explore_music", "arguments": {"source": "both"}}],
    },
    {
        "name": "typo_local_queue",
        "prompt": "ba qinghuaci fang dao Q houmian, zhoujielun de。",
        "calls": [{"tool": "play_music", "arguments": {
            "kind": "song", "mode": "append",
        }}],
    },
    {
        "name": "tv_remote_back",
        "prompt": "电视返回一下。",
        "calls": [{"tool": "tv_remote", "arguments": {"action": "back"}}],
    },
    {
        "name": "music_rack_and_personal_mix",
        "prompt": ("切到音乐模式，功放音量调到65，Mac mini调到75，打开随机播放，"
                   "然后播放我最常听的周杰伦，20首。"),
        "calls": [
            {"tool": "music_mode", "arguments": {}},
            {"tool": "set_volume", "arguments": {"device": "amp", "level": 65}},
            {"tool": "set_volume", "arguments": {"device": "mini", "level": 75}},
            {"tool": "music_transport", "arguments": {"action": "shuffle_on"}},
            {"tool": "play_personal_artist", "arguments": {
                "artist": "周杰伦", "limit": 20, "mode": "replace",
            }},
        ],
    },
    {
        "name": "thirty_song_mixed_queue",
        "prompt": ("在我的资料库里找一些快乐的歌曲，也在 Apple Music 里找一些快乐的"
                   "歌曲，弄30首，把它们混在一起放到Q后面，不要改我的资料库。"),
        "calls": [
            {"tool": "music_transport", "arguments": {"action": "shuffle_on"}},
            {"tool": "curate_music", "arguments": {"mode": "append"},
             "candidate_count": [25, 30]},
        ],
        "forbidden_tools": ["play_music_service", "add_added_to_playlist"],
    },
    {
        "name": "chinese_style_twelve",
        "prompt": "找12首周杰伦经典中国风，本地和Apple Music都有就混着，直接播放。",
        "calls": [
            {"tool": "music_transport", "arguments": {"action": "shuffle_on"}},
            {"tool": "curate_music", "arguments": {"mode": "replace"},
             "candidate_count": [10, 12]},
        ],
    },
    {
        "name": "ordered_curated_sequence",
        "prompt": "按青花瓷、東風破、菊花台这个顺序播放，不要随机。",
        "calls": [
            {"tool": "music_transport", "arguments": {"action": "shuffle_off"}},
            {"tool": "curate_music", "arguments": {"mode": "replace"},
             "candidate_count": [3, 3]},
        ],
    },
    {
        "name": "shared_recent_playlist",
        "prompt": "把这周新加的歌建个播放列表，我要分享给别人，先不要播放。",
        "calls": [{"tool": "add_added_to_playlist", "arguments": {
            "period": "this_week",
        }}],
    },
    {
        "name": "movie_rack_compound",
        "prompt": "电视开机切到HDMI2，功放也开机并切到DAC，最后把功放设到45。",
        "calls": [
            {"tool": "set_power", "arguments": {"device": "tv", "state": "on"}},
            {"tool": "set_input", "arguments": {"device": "tv", "input": "hdmi2"}},
            {"tool": "set_power", "arguments": {"device": "amp", "state": "on"}},
            {"tool": "set_input", "arguments": {"device": "amp", "input": "dac"}},
            {"tool": "set_volume", "arguments": {"device": "amp", "level": 45}},
        ],
    },
    {
        "name": "whole_rack_off",
        "prompt": "别播了，清空队列，然后把整个机架全部关掉。",
        "calls": [{"tool": "everything_off", "arguments": {}}],
    },
    {
        "name": "explicit_album_import_only",
        "prompt": "把 Apple Music 里的 Kind of Blue 专辑加进我的资料库，但不要播放。",
        "calls": [{"tool": "play_music_service", "arguments": {
            "query": "Kind of Blue", "artist": "Miles Davis",
            "kind": "album", "mode": "add_only",
        }}],
    },
    {
        "name": "apple_music_discovery",
        "prompt": "去 Apple Music 看看最近有什么值得听的日语歌，先列给我，不要播放。",
        "calls": [{"tool": "curate_music", "arguments": {
            "source": "service", "mode": "inspect",
        }, "candidate_count": [5, 30]}],
    },
    {
        "name": "pure_personal_explore",
        "prompt": "Surprise me with something based on my taste. Show me first.",
        "calls": [{"tool": "explore_music", "arguments": {
            "source": "both",
        }}],
    },
    {
        "name": "mood_novelty_catalog_play",
        "prompt": "我今天很丧，去 Apple Music 找20首让我振奋、而且我没听过的歌，直接播放。",
        "calls": [{"tool": "curate_music", "arguments": {
            "source": "service", "exclude_played": True,
            "mode": "replace",
        }, "candidate_count": [15, 20]}],
    },
    {
        "name": "inspect_queue_next",
        "prompt": "Q里面现在有什么？下一首是什么？",
        "calls": [{"tool": "inspect_queue", "arguments": {}}],
    },
    {
        "name": "open_music_explore",
        "prompt": "打开 Music 里面的 Explore 给我看看。",
        "calls": [{"tool": "show_panel", "arguments": {
            "panel": "music", "music_view": "explore",
        }}],
    },
    {
        "name": "ambiguous_reference_clarifies",
        "prompt": "把那些放到Q里。",
        "calls": [],
        "requires_text": True,
    },
    {
        "name": "question_does_not_act",
        "prompt": "如果我让你把功放调到70，你会怎么做？先不要动。",
        "calls": [],
        "requires_text": True,
    },
]
