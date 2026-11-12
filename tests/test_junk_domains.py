"""Junk landing-domain detection: random-looking names, domains shared across
unrelated advertisers, and multiple domains per video. Junk domains must never be
discovery seeds and must be rejected by screening."""
import junk_domains as J


# ---------- signal 1: random-looking names ----------

def test_looks_random_flags_noise_strings():
    assert J.looks_random("qatteeey") is True      # q-not-u + tripled e
    assert J.looks_random("mnyyoidy") is True       # improbable 'yy'
    assert J.looks_random("zzzxcvb") is True         # improbable double / consonant run


def test_looks_random_leaves_real_brands_alone():
    for ok in ["ridge", "blissy", "chemicalguys", "coolina", "mudwtr", "ourplace",
               "revomadic", "nortex", "convenity", "hollowsocks", "awolvision", "patriots"]:
        assert J.looks_random(ok) is False, ok
    assert J.looks_random("auk") is False            # too short to judge


# ---------- signal 2: shared across unrelated advertisers ----------

def _adv(aid, *videos):
    return {"advertiser": {"google_advertiser_id": aid},
            "videos": [{"video_id": v, "landing_domain": d} for v, d in videos]}


def test_shared_domain_across_advertisers_is_junk():
    advs = [
        _adv("AR1", ("a", "shared-xyz.com"), ("b", "ridge.com")),
        _adv("AR2", ("c", "shared-xyz.com")),            # same domain, different advertiser
    ]
    junk = J.find_junk_domains(advs)
    assert "shared-xyz.com" in junk
    assert "ridge.com" not in junk                       # unique to one advertiser, real-looking


def test_single_advertiser_realname_domain_is_not_junk():
    advs = [_adv("AR1", ("a", "ridge.com"), ("b", "ridge.com"))]   # one advertiser, repeated
    assert J.find_junk_domains(advs) == set()


# ---------- signal 3: multiple domains per video ----------

def test_multiple_domains_per_video_is_junk():
    advs = [{"advertiser": {"google_advertiser_id": "AR1"}, "videos": [
        {"video_id": "v1", "landing_domain": "aaa.com", "landing_domains": ["aaa.com", "bbb.com"]}]}]
    junk = J.find_junk_domains(advs)
    assert "aaa.com" in junk and "bbb.com" in junk


# ---------- never flag our own store ----------

def test_own_domain_never_junk_even_if_shared():
    advs = [
        _adv("AR1", ("a", "coolinastore.com")),
        _adv("AR2", ("b", "coolinastore.com")),
    ]
    assert "coolinastore.com" not in J.find_junk_domains(advs)


# ---------- is_junk helper ----------

def test_is_junk_matches_set_and_random_label():
    junk = {"shared-xyz.com"}
    assert J.is_junk("http://shared-xyz.com/path", junk) is True
    assert J.is_junk("www.qatteeey.com", junk) is True       # random on its own, even if unseen
    assert J.is_junk("ridge.com", junk) is False
    assert J.is_junk("coolinastore.com", junk) is False      # ours
