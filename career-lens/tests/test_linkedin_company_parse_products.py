"""Focused coverage for LinkedIn company Product-tab parsing."""

from __future__ import annotations

import json

from career_lens.linkedin.parse.company import parse_company_html

COMPANY_URL = "https://www.linkedin.com/company/acme/"

HOME_HTML = """
<html><body><main><section aria-label="Primary content">
  <section class="org-top-card">
    <h1 class="org-top-card-summary__title">Acme</h1>
  </section>
</section></main></body></html>
"""

PRODUCT_HTML = """
<html><body><main><section aria-label="Primary content">
  <code style="display: none">{
    "included": [{
      "$type": "com.linkedin.voyager.organization.OrganizationProduct",
      "mediaSections": [{
        "mediaContent": {
          "embeddedVideo": {
            "url": "https://www.youtube.com/watch?v=architecture456",
            "thumbnail": {
              "digitalmediaAsset": "urn:li:digitalmediaAsset:C4D27AQArchitectureAsset"
            }
          }
        },
        "mediaSource": "YOUTUBE"
      }, {
        "mediaContent": {
          "embeddedVideo": {
            "url": "https://www.youtube.com/watch?v=demo123",
            "thumbnail": {
              "digitalmediaAsset": "urn:li:digitalmediaAsset:C4D27AQDemoAsset"
            }
          }
        },
        "mediaSource": "YOUTUBE"
      }]
    }]
  }</code>
  <section class="products-about__card-spacing">
    <h2 class="products-about__consistant-ui-card-header">What is Acme Flow?</h2>
    <p class="products-about__consistant-ui-card-description">
      Acme Flow automates dependable work.
    </p>
    <section class="products-microsurvey-container">
      <h3>How do you know this product?</h3>
      <button>User</button><button>Potential buyer</button><button>Neither</button>
    </section>
  </section>
  <section>
    <button aria-label="View product demo">
      <div class="products-media-section__featured-item">
        <div class="products-media-section__video-icon-wrapper"></div>
        <img class="products-media-section__thumbnail"
             src="https://media.example/C4D27AQDemoAsset/demo.png"
             alt="Product demo"/>
      </div>
    </button>
    <button aria-label="View architecture">
      <div class="products-media-section__sub-item">
        <div class="products-media-section__video-icon-wrapper"></div>
        <img class="products-media-section__thumbnail"
             src="https://media.example/C4D27AQArchitectureAsset/architecture.png"
             alt="Architecture"/>
      </div>
    </button>
  </section>
  <section><div class="products-top-rated-features__main">
    <p class="products-top-rated-features__subtitle">
      Based on ratings and reviews mentioning these features on G2
    </p>
    <ul><li class="products-top-rated-features__feature-item">
      <h3 class="products-top-rated-features__feature-item-title">Encryption</h3>
      <p>Protects workflow data.</p>
    </li></ul>
    <a href="https://www.g2.com/products/acme-flow">
      <img alt="Show all features on G2"/>
    </a>
  </div></section>
</section></main></body></html>
"""

MINIMAL_PRODUCT_HTML = """
<html><body><main><section aria-label="Primary content">
  <section class="products-about__card-spacing">
    <h2 class="products-about__consistant-ui-card-header">What is Acme Lite?</h2>
    <p class="products-about__consistant-ui-card-description">A smaller product.</p>
  </section>
</section></main></body></html>
"""

def test_company_product_preserves_only_rendered_product_data() -> None:
    result = parse_company_html(
        HOME_HTML,
        {"product": PRODUCT_HTML},
        company_url=COMPANY_URL,
        tab_urls={"product": f"{COMPANY_URL}product/"},
    )

    assert result["company"]["products"] == {
        "source_url": f"{COMPANY_URL}product/",
        "name": "Acme Flow",
        "description": "Acme Flow automates dependable work.",
        "media": [
            {
                "type": "video",
                "video_url": "https://www.youtube.com/watch?v=demo123",
                "thumbnail_url": "https://media.example/C4D27AQDemoAsset/demo.png",
                "alt_text": "Product demo",
            },
            {
                "type": "video",
                "video_url": "https://www.youtube.com/watch?v=architecture456",
                "thumbnail_url": (
                    "https://media.example/"
                    "C4D27AQArchitectureAsset/architecture.png"
                ),
                "alt_text": "Architecture",
            },
        ],
        "top_rated_features": {
            "description": "Based on ratings and reviews mentioning these features on G2",
            "items": [
                {"name": "Encryption", "description": "Protects workflow data."}
            ],
            "source": {
                "name": "G2",
                "url": "https://www.g2.com/products/acme-flow",
            },
        },
    }
    assert "How do you know this product?" not in json.dumps(result["company"]["products"])
    assert result["section_status"]["products"] == "parsed"
    assert result["sources"]["products"] == {
        "artifact": None,
        "url": f"{COMPANY_URL}product/",
    }

def test_company_product_allows_optional_modules_to_be_absent() -> None:
    result = parse_company_html(
        HOME_HTML,
        {"product": MINIMAL_PRODUCT_HTML},
        company_url=COMPANY_URL,
        tab_urls={"product": f"{COMPANY_URL}product/"},
    )

    assert result["company"]["products"] == {
        "source_url": f"{COMPANY_URL}product/",
        "name": "Acme Lite",
        "description": "A smaller product.",
        "media": [],
        "top_rated_features": None,
    }
