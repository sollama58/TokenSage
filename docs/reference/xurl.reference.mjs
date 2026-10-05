// Parse a pump.fun "twitter" metadata field into a typed X reference.
const X_HOSTS = new Set(['twitter.com','x.com','mobile.twitter.com','mobile.x.com','m.twitter.com',
  'fxtwitter.com','fixupx.com','vxtwitter.com','fixvx.com','twittpr.com','nitter.net','xcancel.com',
  'api.fxtwitter.com','api.vxtwitter.com','d.fxtwitter.com','d.fixupx.com']);
const RESERVED = new Set(['i','home','explore','search','hashtag','settings','notifications','messages',
  'intent','share','login','signup','tos','privacy','compose','communities','lists','status','web']);
const HANDLE = /^[A-Za-z0-9_]{1,15}$/;
const ID = /^\d{1,20}$/;

export function parseXRef(raw) {
  if (raw == null) return { kind: 'empty' };
  let s = String(raw).trim().replace(/^[<"'(\[]+|[>"')\],.]+$/g, '');
  if (!s) return { kind: 'empty' };
  if (/^@?[A-Za-z0-9_]{1,15}$/.test(s) && !/^\d+$/.test(s)) return { kind: 'profile', handle: s.replace(/^@/, '') };
  if (!/^[a-z]+:\/\//i.test(s)) s = 'https://' + s;
  let u; try { u = new URL(s); } catch { return { kind: 'invalid', raw }; }
  const host = u.hostname.toLowerCase().replace(/^www\./, '');
  if (host === 't.co') return { kind: 'shortlink', url: u.href, needsResolve: true };
  if (!X_HOSTS.has(host)) return { kind: 'foreign', host, url: u.href };
  const seg = u.pathname.split('/').filter(Boolean).map(decodeURIComponent);
  const lower = seg.map(x => x.toLowerCase());
  // /i/communities/<id>[/...]
  if (lower[0] === 'i' && lower[1] === 'communities' && ID.test(seg[2] || '')) return { kind: 'community', communityId: seg[2] };
  if (lower[0] === 'communities' && ID.test(seg[1] || '')) return { kind: 'community', communityId: seg[1] };
  // /i/web/status/<id>, /i/status/<id>, /status/<id>, /<h>/status(es)/<id>[/photo/1|/video/1|/analytics...]
  const si = lower.findIndex(x => x === 'status' || x === 'statuses');
  if (si >= 0 && ID.test(seg[si + 1] || '')) {
    const h = si > 0 && HANDLE.test(seg[si - 1]) && !RESERVED.has(lower[si - 1]) ? seg[si - 1] : null;
    return { kind: 'tweet', tweetId: seg[si + 1], handle: h };
  }
  if (lower[0] === 'i' && lower[1] === 'lists' && ID.test(seg[2] || '')) return { kind: 'list', listId: seg[2] };
  if (lower[0] === 'i' && lower[1] === 'user' && ID.test(seg[2] || '')) return { kind: 'profile', userId: seg[2] };
  if (lower[0] === 'intent' && (lower[1] === 'user' || lower[1] === 'follow')) {
    const h = u.searchParams.get('screen_name'); const id = u.searchParams.get('user_id');
    if (h && HANDLE.test(h)) return { kind: 'profile', handle: h };
    if (id && ID.test(id)) return { kind: 'profile', userId: id };
  }
  if (lower[0] === 'search') return { kind: 'search', query: u.searchParams.get('q') || '' };
  if (lower[0] === 'hashtag' && seg[1]) return { kind: 'search', query: '#' + seg[1] };
  if (seg.length >= 1 && HANDLE.test(seg[0]) && !RESERVED.has(lower[0])) return { kind: 'profile', handle: seg[0] };
  if (seg.length === 0) return { kind: 'homepage' };
  return { kind: 'unknown', url: u.href };
}

// cdn.syndication.twimg.com token (same as vercel/react-tweet & vxtwitter calcSyndicationToken)
export const syndicationToken = id => ((Number(id) / 1e15) * Math.PI).toString(36).replace(/(0+|\.)/g, '') || '0';
