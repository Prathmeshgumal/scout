import '@testing-library/jest-dom/vitest'

// Browser shims only apply to jsdom tests; server tests run in plain Node.
if (typeof window !== 'undefined') {
  // jsdom lacks these browser APIs; components (radix, resizable panels, media queries) need them.
  class ResizeObserverStub {
    observe() {}
    unobserve() {}
    disconnect() {}
  }
  globalThis.ResizeObserver ??= ResizeObserverStub as unknown as typeof ResizeObserver

  if (!window.matchMedia) {
    window.matchMedia = ((query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addEventListener() {},
      removeEventListener() {},
      addListener() {},
      removeListener() {},
      dispatchEvent: () => false,
    })) as unknown as typeof window.matchMedia
  }

  Element.prototype.scrollIntoView ??= function () {}
  Element.prototype.hasPointerCapture ??= () => false
  Element.prototype.releasePointerCapture ??= function () {}
}
