"""Verification policy shared by native and server-directed browser recipes."""

AUTOMATIC_SIGNS = (
    "checking if the site connection is secure",
    "checking your browser",
    "please wait while we verify your browser",
    "verified! redirecting",
    "verifying your connection",
)
AUTOMATIC_STATUS = 403
TIMEOUT_MS = 15_000
WIDGETS = ((
    "cloudflare", "iframe[src*='challenges.cloudflare.com']",
    ("input[type='checkbox']", "[role='checkbox']", "label"),
),)
# A frame's centre can be its logo, not its control.
CONTROL_INSET = (0.1, 0.5)
WIDGET_GRACE_MS = 4_000
CHALLENGE_STATUS = (403, 429, 503)
CHALLENGE_SIGNS = (
    "just a moment",
    "verify you are human",
    "performing security verification",
    "needs to review the security of your connection",
    "enable javascript and cookies to continue",
)
CLEARED_SIGNS = AUTOMATIC_SIGNS + CHALLENGE_SIGNS
CLICK_TIMEOUT_MS = 5_000
READ_PAUSE_S = (0.4, 1.1)
POLL_S = 0.4
