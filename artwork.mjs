import {safeImageUrl} from './core.mjs';

export function artworkFit(width, height, boxWidth, boxHeight) {
  if (![width, height, boxWidth, boxHeight].every(value => Number.isFinite(value) && value > 0)) return null;
  const scale = Math.min(boxWidth / width, boxHeight / height);
  const w = width * scale, h = height * scale;
  return {x: (boxWidth - w) / 2, y: (boxHeight - h) / 2, width: w, height: h};
}

// Every alternative is another size of the same provider artwork ID.
// Never search by title here or substitute a different record's picture.
export function artworkCandidates(item) {
  const original = safeImageUrl(typeof item === 'string' ? item : item?.image);
  if (!original) return [];
  if (!original.startsWith('https://')) return [original];
  const url = new URL(original);
  const candidates = [];
  if (url.hostname === 'covers.openlibrary.org' && /^\/b\/(?:id|isbn|olid)\/[^/]+-[LMS]\.jpg$/.test(url.pathname)) {
    url.searchParams.set('default', 'false');
    candidates.push(url.href);
    for (const size of ['M', 'S']) {
      const next = new URL(url);
      next.pathname = next.pathname.replace(/-[LMS]\.jpg$/, `-${size}.jpg`);
      candidates.push(next.href);
    }
  } else {
    candidates.push(original);
    if (url.hostname === 'coverartarchive.org' && /^\/release(?:-group)?\/[a-f0-9-]{36}\/front-(?:250|500|1200)$/.test(url.pathname)) {
      const next = new URL(url);
      next.pathname = next.pathname.replace(/\/front-\d+$/, '/front');
      candidates.push(next.href);
    } else if (/^is\d+(?:-ssl)?\.mzstatic\.com$/.test(url.hostname) && /\/\d+x\d+bb\./.test(url.pathname)) {
      for (const size of [300, 100]) {
        const next = new URL(url);
        next.pathname = next.pathname.replace(/\/\d+x\d+bb\./, `/${size}x${size}bb.`);
        candidates.push(next.href);
      }
    } else if (url.hostname === 'static.tvmaze.com' && url.pathname.startsWith('/uploads/images/medium_portrait/')) {
      const next = new URL(url);
      next.pathname = next.pathname.replace('/medium_portrait/', '/original_untouched/');
      candidates.push(next.href);
    }
  }
  return [...new Set(candidates)].slice(0, 3);
}
