/** Portable browser-global shims for Node versions that expose a read-only navigator. */
export function setNavigator(values = {}) {
  const descriptor = Object.getOwnPropertyDescriptor(globalThis, 'navigator');
  const shim = { ...values };

  if (!descriptor || descriptor.configurable || descriptor.writable) {
    Object.defineProperty(globalThis, 'navigator', {
      configurable: true,
      enumerable: descriptor?.enumerable ?? true,
      writable: true,
      value: shim,
    });
    return shim;
  }

  // Defensive fallback for runtimes whose global navigator cannot be replaced.
  // Only test properties are copied; production browser code is untouched.
  const current = globalThis.navigator;
  for (const [key, value] of Object.entries(values)) {
    try {
      Object.defineProperty(current, key, { configurable: true, enumerable: true, writable: true, value });
    } catch {
      // The caller will see the actual runtime navigator if the property is immutable.
    }
  }
  return current;
}
