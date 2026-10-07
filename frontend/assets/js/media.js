/**
 * NEXORA — media.js
 * Attachment validation, pre-send previews, resumable-friendly uploads with
 * progress + cancellation, and authorized media URL handling.
 *
 * Principles:
 *  - Nothing is uploaded before the user confirms the send.
 *  - Large files are streamed by the browser: we never read them into memory.
 *  - Media URLs are backend-authorized and may expire; we re-resolve on failure.
 *  - Duplicate protection uses a stable client_id per attachment.
 */

import { ApiError, api, apiConfig, createRequestId, resolveMediaUrl, upload } from './api.js';
import { configureApiMediaElement, isApiMediaUrl } from './config.js';
import { getLimits } from './theme.js';
import {
  Emitter,
  captureVideoPoster,
  el,
  formatBytes,
  formatDuration,
  imageDimensions,
  mediaKind,
  uid,
} from './utils.js';
import { icon } from './ui.js';

export const mediaEvents = new Emitter();

/* ============================================================
   Validation against backend-configured limits
   ============================================================ */

/**
 * @param {File} file
 * @param {'image'|'video'|'voice'|'auto'} [expected]
 * @returns {{ok:true,kind:string}|{ok:false,message:string}}
 */
function mimeEssence(value = '') {
  return String(value).split(';', 1)[0].trim().toLowerCase();
}

function allowedMime(allowed, actual) {
  const essence = mimeEssence(actual);
  return !allowed.length || allowed.some((value) => mimeEssence(value) === essence);
}

export function validateFile(file, expected = 'auto') {
  if (!file) return { ok: false, message: 'No file selected.' };
  const limits = getLimits();
  const kind = expected === 'auto' ? mediaKind(file.type) : expected;

  if (kind === 'image') {
    if (!allowedMime(limits.allowedImageTypes, file.type)) {
      return { ok: false, message: 'That image format is not supported.' };
    }
    if (file.size > limits.maxImageBytes) {
      return { ok: false, message: `Images must be ${formatBytes(limits.maxImageBytes)} or smaller.` };
    }
    return { ok: true, kind };
  }

  if (kind === 'video') {
    if (!allowedMime(limits.allowedVideoTypes, file.type)) {
      return { ok: false, message: 'That video format is not supported.' };
    }
    if (file.size > limits.maxVideoBytes) {
      return { ok: false, message: `Videos must be ${formatBytes(limits.maxVideoBytes)} or smaller.` };
    }
    return { ok: true, kind };
  }

  if (kind === 'voice' || kind === 'audio') {
    if (file.size > limits.maxVoiceBytes) {
      return { ok: false, message: `Voice notes must be ${formatBytes(limits.maxVoiceBytes)} or smaller.` };
    }
    return { ok: true, kind: 'voice' };
  }

  return { ok: false, message: 'That file type is not supported.' };
}

/** Accept attribute strings derived from backend configuration. */
export function acceptFor(kind) {
  const limits = getLimits();
  if (kind === 'image') return limits.allowedImageTypes.join(',') || 'image/*';
  if (kind === 'video') return limits.allowedVideoTypes.join(',') || 'video/*';
  if (kind === 'voice') return limits.allowedAudioTypes.join(',') || 'audio/*';
  return '*/*';
}

/* ============================================================
   Draft attachment (pre-send)
   ============================================================ */

/**
 * @typedef {object} MediaDraft
 * @property {string} clientId  stable id reused across retries
 * @property {'image'|'video'|'voice'} kind
 * @property {File|Blob} file
 * @property {string} name
 * @property {number} size
 * @property {string} mimeType
 * @property {string|null} previewUrl object URL (revoked on dispose)
 * @property {Blob|null} poster generated video thumbnail
 * @property {number} duration seconds (video/voice)
 * @property {number|null} width
 * @property {number|null} height
 * @property {() => void} dispose
 */

/**
 * Build a local draft with a preview. Nothing touches the network here.
 * @returns {Promise<MediaDraft>}
 */
export async function createDraft(file, kindHint = 'auto') {
  const kind = kindHint === 'auto' ? mediaKind(file.type) : kindHint;
  const previewUrl = URL.createObjectURL(file);
  const draft = {
    clientId: uid('att'),
    kind: kind === 'audio' ? 'voice' : kind,
    file,
    name: file.name || defaultName(kind, file.type),
    size: file.size,
    mimeType: file.type || '',
    previewUrl,
    poster: null,
    duration: 0,
    width: null,
    height: null,
    dispose() {
      if (previewUrl) URL.revokeObjectURL(previewUrl);
    },
  };

  if (draft.kind === 'image') {
    const dims = await imageDimensions(file);
    if (dims) {
      draft.width = dims.width;
      draft.height = dims.height;
    }
  } else if (draft.kind === 'video') {
    const poster = await captureVideoPoster(file);
    if (poster) {
      draft.poster = poster.blob;
      draft.width = poster.width || null;
      draft.height = poster.height || null;
      draft.duration = Math.round(poster.duration || 0);
    }
  }

  return draft;
}

function defaultName(kind, mime) {
  const ext = (mime || '').split('/')[1]?.split(';')[0] || 'bin';
  const stamp = new Date().toISOString().replace(/[:.]/g, '-');
  const base = kind === 'voice' ? 'voice-note' : kind === 'video' ? 'video' : kind === 'image' ? 'image' : 'file';
  return `${base}-${stamp}.${ext}`;
}

/* ============================================================
   Upload
   ============================================================ */

/** Active uploads keyed by clientId, so a send can be cancelled or retried. */
const activeUploads = new Map();

/**
 * Upload a draft as a message in a conversation.
 * @param {string} conversationId
 * @param {MediaDraft} draft
 * @param {object} options { caption, replyToId, onProgress }
 * @returns {Promise<object>} the created message from the backend
 */
export async function uploadDraft(conversationId, draft, options = {}) {
  const { caption = '', replyToId = null, onProgress } = options;

  if (activeUploads.has(draft.clientId)) {
    // Duplicate protection: never start the same attachment twice.
    return activeUploads.get(draft.clientId).promise;
  }

  const controller = new AbortController();
  const requestId = createRequestId();
  mediaEvents.emit('stage', draft.clientId, { stage: 'started', request_id: requestId });
  const form = new FormData();
  form.append('kind', draft.kind);
  form.append('client_id', draft.clientId);
  form.append('file', draft.file, draft.name);
  if (caption) form.append('caption', caption);
  if (replyToId) form.append('reply_to', String(replyToId));
  if (draft.duration) form.append('duration', String(Math.round(draft.duration)));
  if (draft.width) form.append('width', String(draft.width));
  if (draft.height) form.append('height', String(draft.height));
  if (draft.poster) form.append('poster', draft.poster, 'poster.jpg');

  const promise = upload(`/api/conversations/${encodeURIComponent(conversationId)}/messages/`, form, {
    signal: controller.signal,
    requestId,
    onProgress: (info) => {
      // The transport helper reaches 100% on HTTP 2xx. For messages, wait for
      // the domain response/idempotency key below before showing completion.
      if (info.percent >= 100) return;
      mediaEvents.emit('progress', draft.clientId, info);
      onProgress?.(info);
    },
  })
    .then((message) => {
      mediaEvents.emit('stage', draft.clientId, { stage: 'http_completed', request_id: requestId });
      // A 2xx transport status alone is not a confirmed message. Requiring the
      // idempotency key in the response keeps the optimistic bubble/draft when
      // a proxy returns an empty or malformed 2xx after the server committed.
      if (!message || typeof message !== 'object' || !message.id || message.client_id !== draft.clientId) {
        mediaEvents.emit('stage', draft.clientId, { stage: 'unconfirmed', request_id: requestId, status_class: '2xx' });
        throw new ApiError({
          message: 'The server did not confirm this attachment. Retry to confirm its status.',
          code: 'UPLOAD_UNCONFIRMED',
          status: 0,
          requestId,
        });
      }
      const complete = { loaded: draft.size, total: draft.size, percent: 100 };
      mediaEvents.emit('progress', draft.clientId, complete);
      mediaEvents.emit('stage', draft.clientId, { stage: 'confirmed', request_id: requestId, status_class: '2xx' });
      onProgress?.(complete);
      return message;
    })
    .catch((error) => {
      mediaEvents.emit('stage', draft.clientId, {
        stage: 'failed',
        request_id: error?.requestId || requestId,
        status: Number(error?.status) || 0,
        code: String(error?.code || 'UPLOAD_ERROR').slice(0, 64),
      });
      throw error;
    })
    .finally(() => activeUploads.delete(draft.clientId));

  activeUploads.set(draft.clientId, { controller, promise });
  return promise;
}

export function cancelUpload(clientId) {
  const entry = activeUploads.get(clientId);
  if (!entry) return false;
  entry.controller.abort();
  activeUploads.delete(clientId);
  mediaEvents.emit('cancelled', clientId);
  return true;
}

export function isUploading(clientId) {
  return activeUploads.has(clientId);
}

export function cancelAllUploads() {
  for (const clientId of Array.from(activeUploads.keys())) cancelUpload(clientId);
}

/* ============================================================
   Authorized, expiring media URLs
   ============================================================ */

/** mediaId -> { url, expiresAt } */
const urlCache = new Map();
const inflight = new Map();

function isExpired(expiresAt) {
  if (!expiresAt) return false;
  const t = Date.parse(expiresAt);
  return Number.isFinite(t) && t - Date.now() < 15000; // refresh slightly early
}

function needsSignedCrossOriginMediaUrl(value) {
  return !!(
    value
    && apiConfig.origin
    && apiConfig.origin !== window.location.origin
    && isApiMediaUrl(resolveMediaUrl(value))
  );
}

/**
 * Resolve a usable URL for a media object, re-authorizing through the backend
 * when a cached URL is missing or about to expire. Relay-required production
 * returns the protected relay media route instead of an object-store URL.
 * @param {object} media normalized media object
 * @param {'full'|'thumbnail'} [variant]
 * @returns {Promise<string|null>}
 */
export async function getMediaUrl(media, variant = 'full') {
  if (!media) return null;
  const direct = variant === 'thumbnail' ? media.thumbnailUrl : media.url;

  // AttachmentSerializer's direct URL is an authenticated /api/media/ route.
  // Resolve cross-origin protected media explicitly. Relay-required production
  // keeps the authorized stream on /api/media/ rather than returning an object
  // storage endpoint; media elements use credentialed CORS for this route.
  if (direct && !isExpired(media.expiresAt) && !needsSignedCrossOriginMediaUrl(direct)) {
    return resolveMediaUrl(direct);
  }

  if (!media.id) return resolveMediaUrl(direct);

  const cacheKey = `${media.id}:${variant}`;
  const cached = urlCache.get(cacheKey);
  if (cached && !isExpired(cached.expiresAt)) return cached.url;

  if (inflight.has(cacheKey)) return inflight.get(cacheKey);

  const request = (async () => {
    try {
      const data = await api.media.resolve(media.id);
      const url = resolveMediaUrl(variant === 'thumbnail' ? data?.thumbnail_url || data?.url : data?.url);
      if (url) urlCache.set(cacheKey, { url, expiresAt: data?.expires_at || null });
      return url;
    } catch (error) {
      if (error instanceof ApiError && (error.isForbidden || error.isNotFound)) {
        mediaEvents.emit('unauthorized', media.id, error);
      }
      return null;
    } finally {
      inflight.delete(cacheKey);
    }
  })();

  inflight.set(cacheKey, request);
  return request;
}

export function invalidateMediaUrl(mediaId) {
  for (const key of Array.from(urlCache.keys())) {
    if (key.startsWith(`${mediaId}:`)) urlCache.delete(key);
  }
}

/* ============================================================
   Rendering helpers (used by chat.js)
   ============================================================ */

/**
 * Lazily-loaded image attachment. The full image is only fetched when it
 * scrolls into view; a thumbnail is used whenever the backend provides one.
 */
export function renderImageAttachment(media, { onOpen, alt = 'Image attachment' } = {}) {
  const frame = el('button', {
    type: 'button',
    class: 'media-frame',
    'aria-label': 'Open image in full screen',
  });

  const placeholder = el('div', { class: 'media-frame__placeholder' }, [icon('image', { size: 26 })]);
  frame.append(placeholder);

  if (media?.width && media?.height) {
    frame.style.aspectRatio = `${media.width} / ${media.height}`;
  }

  let loaded = false;
  const load = async () => {
    if (loaded) return;
    loaded = true;
    const url = await getMediaUrl(media, media.thumbnailUrl ? 'thumbnail' : 'full');
    if (!url) {
      showMediaError(frame, 'This image is unavailable.');
      return;
    }
    const img = el('img', { alt, loading: 'lazy', decoding: 'async' });
    configureApiMediaElement(img, url);
    img.src = url;
    img.addEventListener('load', () => placeholder.remove(), { once: true });
    img.addEventListener('error', async () => {
      // The authorization window or media request may have expired; re-resolve once.
      invalidateMediaUrl(media.id);
      const fresh = await getMediaUrl(media, 'full');
      if (fresh && fresh !== url) {
        img.src = fresh;
      } else {
        showMediaError(frame, 'This image is no longer available.');
      }
    }, { once: true });
    frame.append(img);
  };

  observeOnce(frame, load);

  frame.addEventListener('click', async () => {
    const url = await getMediaUrl(media, 'full');
    if (!url) {
      showMediaError(frame, 'This image is no longer available.');
      return;
    }
    onOpen?.({ kind: 'image', url, title: media?.name || '', downloadUrl: media?.downloadUrl ? resolveMediaUrl(media.downloadUrl) : null });
  });

  return frame;
}

/**
 * Video attachment: poster only until the user chooses to play.
 * The video file itself is never downloaded to render the list.
 */
export function renderVideoAttachment(media, { onOpen } = {}) {
  const frame = el('button', {
    type: 'button',
    class: 'media-frame',
    'aria-label': media?.duration ? `Play video, ${formatDuration(media.duration)}` : 'Play video',
  });

  const placeholder = el('div', { class: 'media-frame__placeholder' }, [icon('video', { size: 26 })]);
  frame.append(placeholder);
  frame.append(el('span', { class: 'media-frame__play' }, [icon('play', { size: 22 })]));
  if (media?.duration) {
    frame.append(el('span', { class: 'media-frame__duration', text: formatDuration(media.duration) }));
  }
  if (media?.width && media?.height) {
    frame.style.aspectRatio = `${media.width} / ${media.height}`;
  }

  if (media?.status === 'processing') {
    frame.disabled = true;
    frame.setAttribute('aria-label', 'Video is still processing');
    placeholder.append(el('span', { class: 'text-xs', text: 'Processing…', style: { marginTop: '6px' } }));
  }

  let loaded = false;
  observeOnce(frame, async () => {
    if (loaded) return;
    loaded = true;
    const poster = await getMediaUrl(media, 'thumbnail');
    if (!poster) return; // keep the icon placeholder; do NOT fetch the video
    const img = el('img', { alt: '', loading: 'lazy', decoding: 'async' });
    configureApiMediaElement(img, poster);
    img.src = poster;
    img.addEventListener('load', () => placeholder.remove(), { once: true });
    img.addEventListener('error', () => img.remove(), { once: true });
    frame.insertBefore(img, frame.firstChild);
  });

  frame.addEventListener('click', async () => {
    const url = await getMediaUrl(media, 'full');
    if (!url) {
      showMediaError(frame, 'This video is no longer available.');
      return;
    }
    const poster = await getMediaUrl(media, 'thumbnail');
    onOpen?.({
      kind: 'video',
      url,
      poster,
      title: media?.name || '',
      downloadUrl: media?.downloadUrl ? resolveMediaUrl(media.downloadUrl) : null,
    });
  });

  return frame;
}

function showMediaError(frame, message) {
  frame.textContent = '';
  frame.disabled = true;
  frame.append(el('div', { class: 'media-frame__error', text: message }));
}

/** IntersectionObserver-based single-shot lazy trigger. */
function observeOnce(node, callback) {
  if (!('IntersectionObserver' in window)) {
    callback();
    return;
  }
  const observer = new IntersectionObserver(
    (entries) => {
      for (const entry of entries) {
        if (!entry.isIntersecting) continue;
        observer.disconnect();
        callback();
      }
    },
    { rootMargin: '250px 0px' }
  );
  observer.observe(node);
}

/* ============================================================
   File pickers
   ============================================================ */

/**
 * Open a native file picker.
 * @param {object} options { accept, capture, multiple }
 * @returns {Promise<File[]>}
 */
export function pickFiles({ accept = '*/*', capture = null, multiple = false } = {}) {
  return new Promise((resolve) => {
    const input = el('input', { type: 'file', accept, multiple });
    if (capture) input.setAttribute('capture', capture);
    input.style.display = 'none';
    document.body.append(input);

    let settled = false;
    const finish = (files) => {
      if (settled) return;
      settled = true;
      input.remove();
      resolve(files);
    };

    input.addEventListener('change', () => finish(Array.from(input.files || [])));
    // Cancellation has no reliable event; clean up when focus returns.
    window.addEventListener('focus', () => setTimeout(() => finish(Array.from(input.files || [])), 400), { once: true });

    input.click();
  });
}

export default {
  validateFile,
  createDraft,
  uploadDraft,
  cancelUpload,
  getMediaUrl,
  renderImageAttachment,
  renderVideoAttachment,
  pickFiles,
  acceptFor,
  mediaEvents,
};
