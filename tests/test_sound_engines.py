"""Local Piper fallback, voice-list selection and per-concept procedural scores (sound.py, music.py)."""
import json
from types import SimpleNamespace

from agentpitbench import media, music, sound


def _scenes():
    return [{"kind": "s_market", "dur": 2.0, "caption": "Will it dip?"},
            {"kind": "s_twist", "dur": 6.0, "caption": "Same clue."}]


def test_no_key_uses_the_local_voice(monkeypatch, tmp_path):
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    clip = tmp_path / "l.wav"
    clip.write_bytes(b"x")
    monkeypatch.setattr(sound, "local_available", lambda s: True)
    monkeypatch.setattr(sound, "narrate_line_local",
                        lambda s, text, concept: (clip, 0, {"engine": "piper", "voice": "en_US-ryan-high"}))
    monkeypatch.setattr(media, "duration", lambda p: 1.5)
    rec = sound.voice_scenes(SimpleNamespace(data_dir=tmp_path), _scenes(), "noir")
    assert rec["narrator"]["engine"] == "piper" and rec["credits"] == 0


def test_elevenlabs_failure_revoices_the_whole_film_locally(monkeypatch, tmp_path):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "k")
    good = tmp_path / "e.mp3"
    good.write_bytes(b"x")
    calls = {"n": 0}

    def eleven(s, text, concept):  # first line works, second fails (quota)
        calls["n"] += 1
        return (good, 9, {"engine": "elevenlabs"}) if calls["n"] == 1 else (None, 0, {"engine": "elevenlabs"})
    local = tmp_path / "l.wav"
    local.write_bytes(b"x")
    monkeypatch.setattr(sound, "narrate_line", eleven)
    monkeypatch.setattr(sound, "local_available", lambda s: True)
    monkeypatch.setattr(sound, "narrate_line_local", lambda s, text, concept: (local, 0, {"engine": "piper"}))
    monkeypatch.setattr(media, "duration", lambda p: 1.0)
    scenes = _scenes()
    rec = sound.voice_scenes(SimpleNamespace(data_dir=tmp_path), scenes, "noir")
    assert rec["narrator"]["engine"] == "piper"
    assert all(x["voice"]["path"] == str(local) for x in scenes)  # never a mix of voices


def test_no_engine_means_captions_only(monkeypatch, tmp_path):
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    scenes = _scenes()
    assert sound.voice_scenes(SimpleNamespace(data_dir=tmp_path), scenes, "noir") == {}
    assert not any("voice" in x for x in scenes)


def test_concept_voice_override_list_and_fallback(monkeypatch, tmp_path):
    s = SimpleNamespace(data_dir=tmp_path, motd_voice_overrides={})
    monkeypatch.setattr(sound, "voice_catalog", lambda s: None)
    assert sound.concept_voice(s, "noir")["id"] == sound.VOICES["noir"]["id"]          # no list: hard-coded
    s.motd_voice_overrides = {"noir": "OVERRIDE123"}
    assert sound.concept_voice(s, "noir")["id"] == "OVERRIDE123"
    s.motd_voice_overrides = {}
    listed = [{"id": sound.VOICES["noir"]["id"], "name": "Brian", "category": "premade", "labels": {}, "description": ""}]
    monkeypatch.setattr(sound, "voice_catalog", lambda s: listed)
    assert sound.concept_voice(s, "noir")["id"] == sound.VOICES["noir"]["id"]          # still listed: kept
    other = [{"id": "A", "name": "Chirpy", "category": "premade",
              "labels": {"gender": "female", "age": "young", "accent": "american"}, "description": "bright"},
             {"id": "B", "name": "Grit", "category": "premade",
              "labels": {"gender": "male", "age": "middle aged", "accent": "american"}, "description": "deep gravelly"}]
    monkeypatch.setattr(sound, "voice_catalog", lambda s: other)
    v = sound.concept_voice(s, "noir")                                                   # gone: best label match
    assert v["id"] == "B" and v["tag"] == sound.VOICES["noir"]["tag"]


def test_voice_catalog_caches_a_failed_lookup(monkeypatch, tmp_path):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "k")
    s = SimpleNamespace(data_dir=tmp_path)

    def boom(*a, **k):
        raise OSError("401")
    monkeypatch.setattr(sound.urllib.request, "urlopen", boom)
    assert sound.voice_catalog(s) is None
    assert json.loads((sound.cache_dir(s) / "voices.json").read_text()) == []
    monkeypatch.setattr(sound.urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(AssertionError))
    assert sound.voice_catalog(s) is None  # cached for a day: no second call


def test_concept_presets_differ_and_default_is_untouched():
    base = music.synth(3.0)
    assert music.synth(3.0, concept="unknown") == base
    tracks = {c: music.synth(3.0, concept=c) for c in ("noir", "western", "broadcast")}
    assert len({tuple(round(v, 4) for v in t[::200]) for t in tracks.values()}) == 3
    assert all(len(t) == len(base) and max(abs(v) for v in t) <= 0.9001 for t in tracks.values())
    assert set(music.PRESETS) >= set(sound.VOICES)  # every concept has a procedural score


def test_concept_bed_renders_once_and_is_cached(tmp_path):
    p = music.concept_bed(tmp_path, "western", seconds=4)
    assert p and p.exists() and p.stat().st_size > 0
    mtime = p.stat().st_mtime
    assert music.concept_bed(tmp_path, "western", seconds=4) == p and p.stat().st_mtime == mtime
    assert music.concept_bed(tmp_path, "no-such-concept") is None


def test_soundtrack_without_a_key_uses_the_concept_bed(monkeypatch, tmp_path):
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    seen = {}

    def fake_run(cmd, **k):
        seen["cmd"] = cmd
    monkeypatch.setattr(sound.subprocess, "run", fake_run)
    bed = tmp_path / "proc.m4a"
    bed.write_bytes(b"x")
    monkeypatch.setattr(music, "concept_bed", lambda cache, concept: bed)
    out, rec = sound.soundtrack(SimpleNamespace(data_dir=tmp_path), _scenes(), "western", 9, tmp_path / "o.m4a")
    assert rec["score"]["engine"] == "procedural" and rec["score"]["concept"] == "western"
    assert str(bed) in seen["cmd"]
