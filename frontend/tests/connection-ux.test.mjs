/**
 * Connection UX policy tests.
 *
 *   node --test tests/connection-ux.test.mjs
 *
 * The policy under test: users must NOT see a connection indicator for
 * normal operation or brief blips; only persistent disruption, offline, and
 * give-up states are worth pixels — and recovery is a single subtle hint.
 * The scheduler is injected, so no real timers run.
 */

import assert from 'node:assert/strict';
import test from 'node:test';

const { createConnectionUX } = await import(new URL('../assets/js/connection-ux.js', import.meta.url).href);

/** A synchronous fake scheduler: tasks fire only when the clock is ticked. */
function makeScheduler() {
  let now = 0;
  let id = 0;
  const tasks = new Map();
  return {
    setTimeout(fn, ms) {
      id += 1;
      tasks.set(id, { at: now + ms, fn });
      return id;
    },
    clearTimeout(handle) {
      tasks.delete(handle);
    },
    tick(ms) {
      now += ms;
      for (const [handle, task] of [...tasks.entries()].sort((a, b) => a[1].at - b[1].at)) {
        if (task.at <= now) {
          tasks.delete(handle);
          task.fn();
        }
      }
    },
  };
}

function harness(graceMs = 4000) {
  const scheduler = makeScheduler();
  const views = [];
  const ux = createConnectionUX({
    graceMs,
    recoveredMs: 1500,
    scheduler,
    onChange: (view) => views.push({ ...view }),
  });
  // No event means "nothing changed" — i.e. still hidden — because the
  // machine only emits when the view actually changes.
  return { ux, scheduler, views, last: () => views[views.length - 1] || { mode: 'hidden' } };
}

test('normal operation never shows anything', () => {
  const { ux, views } = harness();
  ux.handleState('connecting');
  ux.handleState('open');
  assert.ok(views.every((v) => v.mode === 'hidden'));
});

test('a blip shorter than the grace period shows nothing', () => {
  const { ux, scheduler, last } = harness(4000);
  ux.handleState('connecting');
  scheduler.tick(3000); // outage lasts 3s — inside the 4s grace
  ux.handleState('open');
  assert.equal(last().mode, 'hidden');
});

test('a persistent disruption appears only after the grace period', () => {
  const { ux, scheduler, views, last } = harness(4000);
  ux.handleState('reconnecting', { attempt: 1 });
  scheduler.tick(1000);
  assert.equal(last().mode, 'hidden', 'still inside the grace period');
  scheduler.tick(3500);
  assert.equal(last().mode, 'disrupted', 'grace elapsed while still disrupted');
  ux.handleState('open');
  assert.equal(last().mode, 'recovered', 'a real recovery after a visible outage is hinted once');
  scheduler.tick(1600);
  assert.equal(last().mode, 'hidden', 'the hint fades by itself');
  // A later clean disconnect→connect cycle with no visible indicator must
  // not flash "recovered" again. (A logout-time close is suppressed by the
  // page layer before it ever reaches this machine.)
  views.length = 0;
  ux.handleState('connecting');
  ux.handleState('open');
  assert.ok(views.length === 0 || views.every((v) => v.mode === 'hidden'));
});

test('offline shows immediately and online returns to quiet', () => {
  const { ux, last } = harness();
  ux.handleOffline();
  assert.equal(last().mode, 'offline');
  ux.handleOnline('reconnecting');
  // Optimistically quiet until the transport confirms; the transport's own
  // state event drives any further view.
  ux.handleState('open');
  assert.equal(last().mode, 'recovered');
});

test('terminal give-up shows the persistent failed indicator', () => {
  const { ux, last } = harness();
  ux.handleState('reconnecting', { attempt: 10 });
  ux.handleState('closed', { code: 'RETRY_LIMIT' });
  assert.equal(last().mode, 'failed');
});

test('a retry after a visible outage stays quiet within a fresh grace', () => {
  const { ux, scheduler, last } = harness(4000);
  ux.handleState('reconnecting');
  scheduler.tick(5000); // shown
  ux.handleState('closed', { code: 'RETRY_LIMIT' });
  ux.handleState('connecting'); // manual Reconnect pressed
  assert.equal(last().mode, 'hidden', 'retrying is good news — go quiet');
  scheduler.tick(5000); // and it is still not working
  assert.equal(last().mode, 'disrupted');
});

test('destroy stops every timer', () => {
  const { ux, scheduler, last } = harness();
  ux.handleState('connecting');
  ux.destroy();
  scheduler.tick(60_000);
  assert.equal(last().mode, 'hidden');
});
