import '@testing-library/jest-dom'

// jsdom doesn't ship a working URL constructor against
// window.location.origin in all versions; provide a stable base so
// api.js's `new URL(..., window.location.origin)` works in tests.
if (typeof window !== 'undefined' && !window.location.origin) {
  Object.defineProperty(window, 'location', {
    value: { origin: 'http://localhost', href: 'http://localhost/' },
    writable: true,
  })
}

// antd's ResponsiveObserver calls window.matchMedia on mount; jsdom
// doesn't implement it, so polyfill a no-op matcher.
if (typeof window !== 'undefined' && !window.matchMedia) {
  window.matchMedia = (query) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  })
}

// ResizeObserver polyfill (ECharts-for-react may query it).
if (typeof globalThis.ResizeObserver === 'undefined') {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  }
}

// jsdom doesn't implement HTMLCanvasElement.getContext, which ECharts
// calls on mount.  Stub a 2d context so the chart initialises without
// the heavy `canvas` native package.
if (typeof HTMLCanvasElement !== 'undefined') {
  HTMLCanvasElement.prototype.getContext = function getContext() {
    return {
      fillRect: () => {},
      clearRect: () => {},
      getImageData: () => ({ data: [] }),
      putImageData: () => {},
      createImageData: () => [],
      setTransform: () => {},
      drawImage: () => {},
      save: () => {},
      fillText: () => {},
      restore: () => {},
      beginPath: () => {},
      moveTo: () => {},
      lineTo: () => {},
      closePath: () => {},
      stroke: () => {},
      translate: () => {},
      scale: () => {},
      rotate: () => {},
      arc: () => {},
      fill: () => {},
      measureText: () => ({ width: 0 }),
      transform: () => {},
      rect: () => {},
      clip: () => {},
      canvas: { width: 0, height: 0 },
    }
  }
}

// localStorage shim (jsdom provides one, but ensure it's clean per-test).
beforeEach(() => {
  if (typeof localStorage !== 'undefined') localStorage.clear()
})
