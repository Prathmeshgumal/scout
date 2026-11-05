"""Browser-neutral capture limits shared by native and remote recipes."""

# A finite document is followed to its end, with this ceiling protecting a
# page that never ends. Kept here so server recipes can use the established
# native value without importing the native browser implementation.
MAX_SCROLL_PX = 60_000
MAX_SCROLL_STEPS = 80
BAND_PX = 12_000
MAX_BANDS = 12

# Infinite feeds deliberately receive the smaller, established read budget.
FEED_SCROLL_PX = 5_000


__all__ = ["BAND_PX", "MAX_BANDS", "FEED_SCROLL_PX", "MAX_SCROLL_PX", "MAX_SCROLL_STEPS"]
