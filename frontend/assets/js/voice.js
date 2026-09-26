/**
 * NEXORA — voice.js
 * Voice-note recording (MediaRecorder) and accessible playback.
 *
 * Recording produces a backend-approved container (WebM/Opus where available,
 * MP4/AAC on Safari). The user never types a filename.
 */

import { getLimits } from './theme.js';
import { Emitter, el, formatDuration, uid } from './utils.js';
import { icon, iconButton, announce } from './ui.js';
import { getMediaUrl } from './media.js';

export const voiceEvents = new Emitter();

/* ============================================================
   Capability detection
   ============================================================ */

const CANDIDATE_TYPES = [
  'audio/webm;codecs=opus',
  'audio/webm',
  'audio/ogg;codecs=opus',
  'audio/mp4;codecs=mp4a.40.2',
  'audio/mp4',
];

export function isRecordingSupported() {
  return !!(navigator.mediaDevices?.getUserMedia && typeof window.MediaRecorder !== 'undefined');
}

function pickMimeType() {
  if (typeof MediaRecorder === 'undefined') return '';
  for (const type of CANDIDATE_TYPES) {
    try {
      if (MediaRecorder.isTypeSupported(type)) return type;
    } catch { /* older implementations throw */ }
  }
  return '';
}

/* ============================================================
   Recorder
   ============================================================ */

export class VoiceRecorder {
  #recorder = null;
  #stream = null;
  #chunks = [];
  #startedAt = 0;
  #pausedTotal = 0;
  #pausedAt = 0;
  #tick = null;
  #maxTimer = null;
  #state = 'idle'; // idle | requesting | recording | paused | stopping

  get state() { return this.#state; }
  get isActive() { return this.#state === 'recording' || this.#state === 'paused'; }

  /** Elapsed recording seconds, excluding paused time. */
  get elapsed() {
    if (!this.#startedAt) return 0;
    const now = this.#state === 'paused' ? this.#pausedAt : Date.now();
    return Math.max(0, (now - this.#startedAt - this.#pausedTotal) / 1000);
  }

  /**
   * Request microphone access and begin recording.
   * @returns {Promise<void>}
   * @throws {Error} with a user-safe message on permission/hardware failure
   */
  async start() {
    if (this.isActive) return;
    if (!isRecordingSupported()) {
      throw new Error('Voice recording is not supported in this browser.');
    }

    this.#state = 'requesting';
    voiceEvents.emit('state', this.#state);

    try {
      this.#stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
      });
    } catch (error) {
      this.#state = 'idle';
      voiceEvents.emit('state', this.#state);
      if (error?.name === 'NotAllowedError' || error?.name === 'SecurityError') {
        throw new Error('Microphone access was blocked. Allow microphone access to record a voice note.');
      }
      if (error?.name === 'NotFoundError') {
        throw new Error('No microphone was found on this device.');
      }
      throw new Error('The microphone could not be started.');
    }

    const mimeType = pickMimeType();
    try {
      this.#recorder = new MediaRecorder(this.#stream, mimeType ? { mimeType, audioBitsPerSecond: 48000 } : undefined);
    } catch {
      this.#teardownStream();
      this.#state = 'idle';
      throw new Error('Voice recording is not supported in this browser.');
    }

    this.#chunks = [];
    this.#recorder.addEventListener('dataavailable', (event) => {
      if (event.data && event.data.size > 0) this.#chunks.push(event.data);
    });
    this.#recorder.addEventListener('error', () => {
      voiceEvents.emit('error', new Error('Recording stopped unexpectedly.'));
      this.cancel();
    });

    this.#recorder.start(250); // timeslice keeps memory bounded
    this.#startedAt = Date.now();
    this.#pausedTotal = 0;
    this.#pausedAt = 0;
    this.#state = 'recording';
    voiceEvents.emit('state', this.#state);
    announce('Recording started.');

    this.#tick = setInterval(() => voiceEvents.emit('tick', this.elapsed), 200);

    const maxSeconds = getLimits().maxVoiceSeconds;
    if (maxSeconds > 0) {
      this.#maxTimer = setTimeout(() => {
        voiceEvents.emit('limit-reached', maxSeconds);
      }, maxSeconds * 1000);
    }
  }

  pause() {
    if (this.#state !== 'recording' || !this.#recorder) return;
    try { this.#recorder.pause(); } catch { return; }
    this.#pausedAt = Date.now();
    this.#state = 'paused';
    voiceEvents.emit('state', this.#state);
  }

  resume() {
    if (this.#state !== 'paused' || !this.#recorder) return;
    try { this.#recorder.resume(); } catch { return; }
    this.#pausedTotal += Date.now() - this.#pausedAt;
    this.#pausedAt = 0;
    this.#state = 'recording';
    voiceEvents.emit('state', this.#state);
  }

  /**
   * Stop and return the recorded clip.
   * @returns {Promise<{blob:Blob,duration:number,mimeType:string,name:string}|null>}
   */
  stop() {
    return new Promise((resolve) => {
      if (!this.#recorder || !this.isActive) {
        resolve(null);
        return;
      }
      const duration = this.elapsed;
      this.#state = 'stopping';
      this.#clearTimers();

      this.#recorder.addEventListener('stop', () => {
        const mimeType = this.#recorder?.mimeType || pickMimeType() || 'audio/webm';
        const blob = new Blob(this.#chunks, { type: mimeType });
        this.#chunks = [];
        this.#teardownStream();
        this.#state = 'idle';
        voiceEvents.emit('state', this.#state);
        announce('Recording stopped.');

        if (!blob.size) {
          resolve(null);
          return;
        }
        resolve({
          blob,
          duration: Math.max(1, Math.round(duration)),
          mimeType,
          name: `voice-${uid('n').slice(-8)}.${extensionFor(mimeType)}`,
        });
      }, { once: true });

      try { this.#recorder.stop(); } catch { resolve(null); }
    });
  }

  /** Discard the recording entirely. */
  cancel() {
    this.#clearTimers();
    if (this.#recorder && this.isActive) {
      try { this.#recorder.stop(); } catch { /* ignore */ }
    }
    this.#chunks = [];
    this.#recorder = null;
    this.#teardownStream();
    this.#state = 'idle';
    voiceEvents.emit('state', this.#state);
    voiceEvents.emit('cancelled');
    announce('Recording cancelled.');
  }

  #clearTimers() {
    clearInterval(this.#tick);
    clearTimeout(this.#maxTimer);
    this.#tick = null;
    this.#maxTimer = null;
  }

  #teardownStream() {
    this.#stream?.getTracks().forEach((track) => track.stop());
    this.#stream = null;
  }
}

function extensionFor(mimeType) {
  const m = String(mimeType).toLowerCase();
  if (m.includes('webm')) return 'webm';
  if (m.includes('ogg')) return 'ogg';
  if (m.includes('mp4')) return 'm4a';
  if (m.includes('mpeg')) return 'mp3';
  return 'webm';
}

/* ============================================================
   Playback — one clip at a time, accessible controls
   ============================================================ */

let activeAudio = null;

/**
 * Build an accessible voice-note player.
 * The audio file is only fetched when the user presses play.
 * @param {object} media normalized media object
 * @param {object} [options] { duration }
 */
export function renderVoicePlayer(media, { duration = 0 } = {}) {
  const totalHint = Number(media?.duration || duration) || 0;

  const root = el('div', { class: 'voice', role: 'group', 'aria-label': 'Voice note' });

  const playBtn = el('button', {
    type: 'button',
    class: 'voice__play',
    'aria-label': 'Play voice note',
  });
  playBtn.append(icon('play', { size: 18 }));

  const track = el('div', {
    class: 'voice__track',
    role: 'slider',
    tabindex: '0',
    'aria-label': 'Playback position',
    'aria-valuemin': '0',
    'aria-valuemax': String(Math.round(totalHint) || 100),
    'aria-valuenow': '0',
    'aria-valuetext': '0 seconds',
  });
  const fill = el('span', { class: 'voice__fill' });
  const knob = el('span', { class: 'voice__knob' });
  track.append(fill, knob);

  const time = el('div', { class: 'voice__time', text: totalHint ? formatDuration(totalHint) : '--:--' });
  const body = el('div', { class: 'voice__body' }, [track, time]);
  root.append(playBtn, body);

  /** @type {HTMLAudioElement|null} */
  let audio = null;
  let loading = false;

  const setProgress = (current, total) => {
    const pct = total > 0 ? Math.min(100, (current / total) * 100) : 0;
    fill.style.width = `${pct}%`;
    knob.style.left = `${pct}%`;
    track.setAttribute('aria-valuenow', String(Math.round(current)));
    track.setAttribute('aria-valuemax', String(Math.round(total) || 100));
    track.setAttribute('aria-valuetext', `${formatDuration(current)} of ${formatDuration(total)}`);
    time.textContent = `${formatDuration(current)} / ${formatDuration(total || totalHint)}`;
  };

  const setPlayingUI = (playing) => {
    playBtn.textContent = '';
    playBtn.append(icon(playing ? 'pause' : 'play', { size: 18 }));
    playBtn.setAttribute('aria-label', playing ? 'Pause voice note' : 'Play voice note');
  };

  async function ensureAudio() {
    if (audio) return audio;
    if (loading) return null;
    loading = true;
    playBtn.disabled = true;
    try {
      const url = await getMediaUrl(media, 'full');
      if (!url) {
        time.textContent = 'Unavailable';
        root.append(el('span', { class: 'sr-only', text: 'This voice note is no longer available.' }));
        return null;
      }
      audio = new Audio();
      audio.preload = 'metadata';
      audio.src = url;

      audio.addEventListener('loadedmetadata', () => {
        const total = Number.isFinite(audio.duration) ? audio.duration : totalHint;
        setProgress(0, total);
      });
      audio.addEventListener('timeupdate', () => {
        const total = Number.isFinite(audio.duration) ? audio.duration : totalHint;
        setProgress(audio.currentTime, total);
      });
      audio.addEventListener('ended', () => {
        setPlayingUI(false);
        setProgress(0, Number.isFinite(audio.duration) ? audio.duration : totalHint);
        activeAudio = null;
      });
      audio.addEventListener('error', () => {
        time.textContent = 'Unavailable';
        setPlayingUI(false);
      });
      return audio;
    } finally {
      loading = false;
      playBtn.disabled = false;
    }
  }

  playBtn.addEventListener('click', async () => {
    const a = await ensureAudio();
    if (!a) return;
    if (!a.paused) {
      a.pause();
      setPlayingUI(false);
      return;
    }
    if (activeAudio && activeAudio !== a) {
      activeAudio.pause();
    }
    try {
      await a.play();
      activeAudio = a;
      setPlayingUI(true);
    } catch {
      setPlayingUI(false);
    }
  });

  const seekTo = (ratio) => {
    if (!audio || !Number.isFinite(audio.duration)) return;
    audio.currentTime = Math.max(0, Math.min(audio.duration, audio.duration * ratio));
  };

  track.addEventListener('click', (event) => {
    const rect = track.getBoundingClientRect();
    seekTo((event.clientX - rect.left) / rect.width);
  });

  track.addEventListener('keydown', (event) => {
    if (!audio || !Number.isFinite(audio.duration)) return;
    const step = 5;
    if (event.key === 'ArrowRight') {
      event.preventDefault();
      audio.currentTime = Math.min(audio.duration, audio.currentTime + step);
    } else if (event.key === 'ArrowLeft') {
      event.preventDefault();
      audio.currentTime = Math.max(0, audio.currentTime - step);
    } else if (event.key === 'Home') {
      event.preventDefault();
      audio.currentTime = 0;
    } else if (event.key === 'End') {
      event.preventDefault();
      audio.currentTime = audio.duration;
    } else if (event.key === ' ' || event.key === 'Enter') {
      event.preventDefault();
      playBtn.click();
    }
  });

  setProgress(0, totalHint);

  return root;
}

/**
 * Preview player for a locally-recorded clip (before sending).
 * @param {Blob} blob
 * @param {number} duration
 */
export function renderVoicePreview(blob, duration) {
  const url = URL.createObjectURL(blob);
  const root = el('div', { class: 'voice', role: 'group', 'aria-label': 'Voice note preview' });
  const audio = new Audio(url);
  audio.preload = 'metadata';

  const playBtn = el('button', { type: 'button', class: 'voice__play', 'aria-label': 'Play recording' });
  playBtn.append(icon('play', { size: 18 }));
  const bar = el('div', { class: 'voice__track' });
  const fill = el('span', { class: 'voice__fill' });
  bar.append(fill);
  const time = el('div', { class: 'voice__time', text: formatDuration(duration) });

  playBtn.addEventListener('click', async () => {
    if (audio.paused) {
      try {
        await audio.play();
        playBtn.textContent = '';
        playBtn.append(icon('pause', { size: 18 }));
        playBtn.setAttribute('aria-label', 'Pause recording');
      } catch { /* autoplay policy */ }
    } else {
      audio.pause();
      playBtn.textContent = '';
      playBtn.append(icon('play', { size: 18 }));
      playBtn.setAttribute('aria-label', 'Play recording');
    }
  });

  audio.addEventListener('timeupdate', () => {
    const total = Number.isFinite(audio.duration) ? audio.duration : duration;
    fill.style.width = `${Math.min(100, (audio.currentTime / total) * 100)}%`;
    time.textContent = `${formatDuration(audio.currentTime)} / ${formatDuration(total)}`;
  });
  audio.addEventListener('ended', () => {
    playBtn.textContent = '';
    playBtn.append(icon('play', { size: 18 }));
    playBtn.setAttribute('aria-label', 'Play recording');
    fill.style.width = '0%';
  });

  root.append(playBtn, el('div', { class: 'voice__body' }, [bar, time]));
  root.dispose = () => {
    audio.pause();
    URL.revokeObjectURL(url);
  };
  return root;
}

export default { VoiceRecorder, isRecordingSupported, renderVoicePlayer, renderVoicePreview, voiceEvents };
