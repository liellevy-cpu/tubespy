"""Own-brand exclusion config: names, domains, and advertiser-list filtering."""
import own_brands


def test_own_brand_names_match_case_insensitively_and_as_substrings():
    assert own_brands.is_own_brand_name("Coolina")
    assert own_brands.is_own_brand_name("COOLINA LLC")
    assert own_brands.is_own_brand_name("Coolina Store")
    assert own_brands.is_own_brand_name("Emater")
    assert own_brands.is_own_brand_name("Bodywell")
    assert not own_brands.is_own_brand_name("Super Brand Tools")
    assert not own_brands.is_own_brand_name(None)


def test_own_domains_match_registrable_and_label_variants():
    assert own_brands.is_own_domain("coolinastore.com")
    assert own_brands.is_own_domain("www.coolinastore.com")
    assert own_brands.is_own_domain("coolina.com")          # label carries an owned token
    assert not own_brands.is_own_domain("ramenbuy.com")
    assert not own_brands.is_own_domain("")


def test_is_own_combines_name_or_domain():
    assert own_brands.is_own(name="Coolina")
    assert own_brands.is_own(domain="coolinastore.com")
    assert not own_brands.is_own(name="Ridge", domain="ridge.com")


def test_drop_own_advertisers_removes_owned_brands():
    advertisers = [
        {"advertiser": {"name": "Super Brand Tools"}, "videos": []},
        {"advertiser": {"name": "Coolina"}, "videos": []},
        {"advertiser": {"name": "AUK"}, "videos": []},
    ]
    kept = [a["advertiser"]["name"] for a in own_brands.drop_own_advertisers(advertisers)]
    assert kept == ["Super Brand Tools", "AUK"]
