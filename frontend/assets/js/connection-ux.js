/**
 * NEXORA — connection-ux.js
 * Decides what the user should *see* for a given realtime connection state.
 *
 * Problem this solves: the transport legitimately changes state often (a page
 * load, a token expiry, a mobile tab switch, a brief network blip). Painting
 * a banner on every change made the app feel like it was "Reconnecting…"
 * every few seconds even though messages kept working.
 *
 * Policy (see the product requirement):
 *   Normal operation            → nothing is shown. Ever.
 *   Brief interruption (<grace) → nothing is shown; most blips resolve here.
 *   Persistent disruption       → a compact, non-blocking indicator appears
 *                                 only after the grace period elapses.
 *   Offline (browser says so)   → a compact offline indicator, immediately.
 *   Gave up (retry limit / auth)→ a persistent indicator with a retry action.
 *   Recovered                   → a subtle, one-time "Back online" hint that
 *                                 fades by itself; never flashes repeatedly.
 *
 * This module has no DOM and no imports: it is a pure state machine so it can
 * be unit-tested exhaustively (see tests/connection-ux.test.mjs).
 */

/**
 * @param {object} [options]
 * @param {number} [options.graceMs]        quiet period before an indicator appears
 * @param {number} [options.recoveredMs]    how long the recovery hint stays
 * @param {{setTimeout:Function, clearTimeout:Function}} [options.scheduler]
 * @param {(view:{mode:'hidden'|'offline'|'disrupted'|'failed'|'recovered', detail?:object})=>void} options.onChange
 */
export function createConnectionUX(options = {}) {
  const {
    graceMs = 4000,
    recoveredMs = 1500,
    scheduler = { setTimeout, clearTimeout },
    onChange = () => {},
  } = options;

  /** @type {'hidden'|'offline'|'disrupted'|'failed'|'recovered'} */
  let mode = 'hidden';
  let graceTimer = null;
  let recoveredTimer = null;
  let visibleBefore = false; // has an indicator been shown for this outage?
  let lastDetail = null;

  const emit = (next, detail = null) => {
    mode = next;
    lastDetail = detail;
    onChange({ mode, detail });
  };

  const clearGrace = () => {
    if (graceTimer) scheduler.clearTimeout(graceTimer);
    graceTimer = null;
  };
  const clearRecovered = () => {
    if (recoveredTimer) scheduler.clearTimeout(recoveredTimer);
    recoveredTimer = null;
  };

  const showAfterGrace = (detail) => {
    if (graceTimer) return; // already pending — one timer, always
    graceTimer = scheduler.setTimeout(() => {
      graceTimer = null;
      // The outage outlasted the grace period: this is worth showing now.
      visibleBefore = true;
      emit('disrupted', detail);
    }, graceMs);
  };

  return {
    /** Current view mode. */
    get mode() {
      return mode;
    },

    /** Feed a realtime transport state in ('idle' ignored). */
    handleState(state, detail = null) {
      switch (state) {
        case 'connecting':
        case 'reconnecting':
          if (mode === 'failed' || mode === 'offline') {
            // Coming back from a terminal/offline view: retrying is good
            // news, but stay quiet unless it takes longer than the grace.
            clearRecovered();
            visibleBefore = true;
            mode = 'hidden';
            onChange({ mode: 'hidden' });
          }
          showAfterGrace(detail);
          return;

        case 'open':
          clearGrace();
          clearRecovered();
          if (visibleBefore) {
            // A real recovery after a *visible* outage: one subtle hint.
            visibleBefore = false;
            emit('recovered', detail);
            recoveredTimer = scheduler.setTimeout(() => {
              recoveredTimer = null;
              if (mode === 'recovered') emit('hidden');
            }, recoveredMs);
          } else if (mode !== 'hidden') {
            emit('hidden');
          }
          return;

        case 'offline':
          clearGrace();
          clearRecovered();
          visibleBefore = true;
          emit('offline', detail);
          return;

        case 'closed':
          clearGrace();
          clearRecovered();
          // 'closed' from the transport is terminal (stop()/auth/give-up).
          // Only the give-up flavour deserves the persistent indicator;
          // stop() during logout navigates away anyway.
          visibleBefore = true;
          emit('failed', detail);
          return;

        default:
          return;
      }
    },

    /** The browser says the device went offline. */
    handleOffline() {
      this.handleState('offline');
    },

    /** The browser says connectivity returned. */
    handleOnline(transportState) {
      // Optimistic quietness: let the transport confirm with 'open'. If the
      // transport is down, its own state event will drive the view again.
      clearRecovered();
      if (mode === 'offline') {
        visibleBefore = true; // it *was* visible; recovery deserves the hint
        mode = 'hidden';
        onChange({ mode: 'hidden' });
      }
      if (transportState && transportState !== 'open') this.handleState(transportState);
    },

    /** Stop all timers (page teardown). */
    destroy() {
      clearGrace();
      clearRecovered();
      mode = 'hidden';
    },
  };
}

export default createConnectionUX;
