import json
from pathlib import Path

import pytest

from registry_sync import RegistryError, parse_registry_csv


SHEET_URL = "docs.google.com/spreadsheets/d/"


def test_parse_registry_csv_builds_an_active_safe_registry():
    csv_text = (
        "Advertiser,Google Ads Transparency URL,Active\n"
        "Ridge,https://adstransparency.google.com/advertiser/AR14056452383756517377?region=US&platform=YOUTUBE,TRUE\n"
        "Removed,https://adstransparency.google.com/advertiser/AR00000000000000000000?platform=YOUTUBE,FALSE\n"
    )

    registry = parse_registry_csv(csv_text, spreadsheet_id="sheet-id", sheet_gid="123")

    assert registry["schema_version"] == 2
    assert registry["source"]["spreadsheet_id"] == "sheet-id"
    assert registry["source"]["sheet_gid"] == "123"
    assert registry["advertisers"] == [
        {
            "name": "Ridge",
            "source": "sheet",
            "transparency_url": "https://adstransparency.google.com/advertiser/AR14056452383756517377?region=US&platform=YOUTUBE",
        }
    ]


@pytest.mark.parametrize(
    "csv_text, message",
    [
        (
            "Advertiser,Google Ads Transparency URL,Active\nRidge,https://evil.example/advertiser/AR1,TRUE\n",
            "unsafe",
        ),
        (
            "Advertiser,Google Ads Transparency URL,Active\nRidge,https://adstransparency.google.com/advertiser/AR1?platform=YOUTUBE,TRUE\nridge,https://adstransparency.google.com/advertiser/AR2?platform=YOUTUBE,TRUE\n",
            "duplicate",
        ),
        (
            "Advertiser,Google Ads Transparency URL,Active\n,https://adstransparency.google.com/advertiser/AR1,TRUE\n",
            "missing advertiser",
        ),
    ],
)
def test_parse_registry_csv_fails_closed(csv_text, message):
    with pytest.raises(RegistryError, match=message):
        parse_registry_csv(csv_text, spreadsheet_id="sheet-id", sheet_gid="123")


def test_dashboard_has_add_only_form_and_exact_link_guide():
    project = Path(__file__).resolve().parents[1]
    html = (project / "public" / "index.html").read_text(encoding="utf-8")
    assert 'id="add-brand-form"' in html
    assert 'id="brand-name"' in html and 'id="brand-url"' in html
    assert 'https://adstransparency.google.com/advertiser/AR' in html
    assert 'address bar' in html and 'weekly' in html
    assert 'Add or delete rows' not in html and SHEET_URL not in html
    assert 'id="brand-status"' in html and 'aria-live="polite"' in html


def test_add_form_has_responsive_styles_and_coherent_release():
    import re
    project=Path(__file__).resolve().parents[1]
    css=(project/'public/styles.css').read_text()
    html=(project/'public/index.html').read_text()
    assert '.add-brand-panel' in css and '#add-brand-form' in css
    versions=re.findall(r'(?:styles.css|app.js)\?v=([^\"]+)',html)
    assert versions==['20261109-redesign-1','20261109-redesign-1']


def test_frontend_posts_only_add_route_and_disables_repeat_submit():
    js = (Path(__file__).resolve().parents[1] / 'public/app.js').read_text()
    assert "fetch('/api/add-brand'" in js
    assert 'submit.disabled = true' in js
    assert 'textContent' in js
    assert "method: 'DELETE'" not in js and "method: 'PATCH'" not in js
    assert "fetch('/api/brands'" in js
