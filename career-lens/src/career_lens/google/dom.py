"""Keep the operator's own identity out of what a SERP capture stores."""

from __future__ import annotations

from patchright.sync_api import Page

# The account bar. Signed out it holds the apps grid and a "Sign in" link;
# signed in it holds the avatar, the display name and the account's email
# address, all of which name the operator rather than the search.
#
# `#gb` is the whole bar and the search form is outside it, so hiding it costs
# the capture nothing it is being taken for. The classes around it (`gb_y`,
# `gb_Nd`) are generated and change between Google deploys; the id has not.
STRIP_ACCOUNT = """() => {
  const host = location.hostname;
  if (!/(^|\\.)google\\.[a-z.]+$/.test(host)) return 0;
  let gone = 0;
  const hide = el => {
    if (el.hasAttribute('data-career-lens-artifact-only')) return;
    el.setAttribute('data-career-lens-artifact-only', '');
    el.style.setProperty('display', 'none', 'important');
    gone++;
  };

  // #searchform is sticky on the result page. Chrome's full-page compositor
  // can paint it a second time partway down a screenshot, even though it is
  // present once in the DOM. Results are outside this container.
  for (const el of document.querySelectorAll('header#gb, #searchform')) hide(el);
  return gone;
}"""


# The footer, which Google puts under every result page: the country, the
# postcode and city its geolocation resolved, and "From your IP address"
# saying so outright. It is the operator's location, not the query's, and it
# would otherwise sit in the screenshot, the text and the stored HTML of every
# search ever run.
#
# `#footcnt` also holds the Privacy and Terms links, which are boilerplate --
# the whole container goes. Pagination is a separate one, and the recipe
# captures a single unscrolled SERP without paginating regardless.
STRIP_LOCATION = """() => {
  const host = location.hostname;
  if (!/(^|\\.)google\\.[a-z.]+$/.test(host)) return 0;
  let gone = 0;
  const hide = el => {
    if (el.hasAttribute('data-career-lens-artifact-only')) return;
    el.setAttribute('data-career-lens-artifact-only', '');
    el.style.setProperty('display', 'none', 'important');
    gone++;
  };

  for (const el of document.querySelectorAll('#footcnt')) hide(el);
  // Google also offers the location inline on some result surfaces. The
  // element is its own tag rather than a generated class, so it is named
  // directly, and anything already inside a hidden footer is skipped.
  for (const el of document.querySelectorAll('update-location')) {
    if (!el.closest('[data-career-lens-artifact-only]')) hide(el);
  }
  return gone;
}"""


def strip_personal(page: Page) -> int:
    """Hide the signed-in account and the IP-derived location.

    Safe on any page and at any point, for the same two reasons the LinkedIn
    strip is: it does nothing away from Google, and hiding what is already
    hidden is not counted twice. Both matter, because `capture_page` calls this
    once before every screenshot and again before the read that decides what
    the text and the HTML carry.
    """
    gone = 0
    for script in (STRIP_ACCOUNT, STRIP_LOCATION):
        try:
            gone += int(page.evaluate(script))
        except Exception:
            # A page that cannot be asked is a page mid-navigation. The next
            # call gets it, and there is always one before anything is stored.
            continue
    return gone
