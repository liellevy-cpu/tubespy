"""English gate (language._detect): the spoken language is decided from the video's
own ASR/audio/transcript signals, NEVER from the uploader's defaultAudioLanguage
alone, and anything unresolved is excluded (None). Offline: the YouTube player and
caption fetches are stubbed."""
import language as L
import pytest


def _player(*, meta=None, captions=None, audio=None, default_index=None):
    renderer = {}
    if captions is not None:
        renderer["captionTracks"] = captions
    if audio is not None:
        renderer["audioTracks"] = audio
    if default_index is not None:
        renderer["defaultAudioTrackIndex"] = default_index
    return {"videoDetails": {"defaultAudioLanguage": meta},
            "captions": {"playerCaptionsTracklistRenderer": renderer}}


def cap(code, kind=None, url=None):
    t = {"languageCode": code, "baseUrl": url or f"https://x/{code}"}
    if kind:
        t["kind"] = kind
    return t


@pytest.fixture
def stub(monkeypatch):
    """Install a player dict and an optional transcript-text map."""
    def install(player, texts=None):
        monkeypatch.setattr(L, "_player", lambda vid: player)
        monkeypatch.setattr(L, "_caption_text", lambda url, limit=1200: (texts or {}).get(url, ""))
    return install


# ---- helpers ----

def test_base_and_is_en():
    assert L._base("en-US.10") == "en" and L._base("es") == "es" and L._base(None) is None
    assert L.is_en("en") and L.is_en("en-GB") and not L.is_en("es") and not L.is_en(None)


# ---- the core rule: metadata alone is never enough ----

def test_default_audio_language_alone_is_not_enough(stub):
    # Uploader says English but there is NO speech evidence at all -> exclude, not True.
    stub(_player(meta="en"))
    assert L._detect("v")["english"] is None
    assert L._detect("v")["method"] == "insufficient_signal"


def test_metadata_en_does_not_rescue_a_video_with_no_captions(stub):
    stub(_player(meta="en", captions=[]))
    assert L._detect("v")["english"] is None


# ---- ASR caption language is trusted first ----

def test_single_asr_english(stub):
    stub(_player(meta=None, captions=[cap("en", "asr")]))
    r = L._detect("v")
    assert r["english"] is True and r["method"] == "asr_caption"


def test_single_asr_spanish(stub):
    stub(_player(meta="en", captions=[cap("es", "asr")]))   # metadata lies "en"
    r = L._detect("v")
    assert r["english"] is False and r["lang"] == "es"


# ---- auto-dubbed videos: the default audio track anchors the original speech ----

def test_autodub_spanish_origin_excluded(stub):
    # The real bug: Spanish original, auto-dubbed to English, metadata often "en".
    stub(_player(meta=None,
                 captions=[cap("en-US", "asr"), cap("es", "asr")],
                 audio=[{"audioTrackId": "en-US.10"}, {"audioTrackId": "es-US.4"}],
                 default_index=1))
    r = L._detect("v")
    assert r["english"] is False and r["method"] == "asr_matches_default_audio"


def test_autodub_english_origin_passes(stub):
    # English original that offers a Spanish dub -> default audio is English -> keep.
    stub(_player(meta="en",
                 captions=[cap("en", "asr"), cap("es", "asr")],
                 audio=[{"audioTrackId": "en.10"}, {"audioTrackId": "es.4"}],
                 default_index=0))
    assert L._detect("v")["english"] is True


def test_autodub_no_asr_uses_default_audio_track(stub):
    stub(_player(meta="en",
                 audio=[{"audioTrackId": "en.10"}, {"audioTrackId": "de.4"}],
                 default_index=1, captions=[]))
    r = L._detect("v")
    assert r["english"] is False and r["method"] == "default_audio_track"


# ---- conflict between speech and metadata is resolved by transcript, else excluded ----

def test_asr_en_but_metadata_other_confirmed_by_transcript(stub):
    url = "https://x/en"
    stub(_player(meta="es", captions=[cap("en", "asr", url=url)]),
         texts={url: "this is a genuine english voiceover describing the product " * 3})
    r = L._detect("v")
    assert r["english"] is True and r["method"] == "asr_confirmed_by_transcript"


def test_asr_en_but_metadata_other_unconfirmed_is_excluded(stub):
    # ASR claims English, metadata says Spanish, transcript has no usable text -> exclude.
    stub(_player(meta="es", captions=[cap("en", "asr")]), texts={})
    r = L._detect("v")
    assert r["english"] is None and r["method"] == "conflict_speech_vs_metadata"


def test_transcript_overrides_asr_when_text_is_other_language(stub):
    url = "https://x/en"
    stub(_player(meta="es", captions=[cap("en", "asr", url=url)]),
         texts={url: "hola amigos este es un video en espanol sobre el producto nuevo " * 3})
    r = L._detect("v")
    assert r["english"] is False and r["method"] == "transcript_overrides_metadata"


# ---- single manual caption + transcript corroboration ----

def test_single_manual_caption_conflicts_with_transcript_excluded(stub):
    url = "https://x/en"
    stub(_player(meta=None, captions=[cap("en", url=url)]),
         texts={url: "bonjour ceci est une video entierement en francais sur le produit " * 3})
    assert L._detect("v")["english"] is None


def test_single_manual_english_caption_passes(stub):
    url = "https://x/en"
    stub(_player(meta=None, captions=[cap("en", url=url)]),
         texts={url: "hello everyone welcome to this english review of the product today " * 3})
    assert L._detect("v")["english"] is True
