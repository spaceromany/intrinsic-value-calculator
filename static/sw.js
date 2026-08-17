// 캐시 버전. 값을 바꾸면 activate에서 이전 캐시를 전부 삭제한다.
const CACHE_VERSION = 'v2';
const CACHE_NAME = `intrinsic-value-calculator-${CACHE_VERSION}`;

// 오프라인 대비로 미리 받아둘 정적 자원.
// HTML('/')은 일부러 넣지 않는다. 이전 버전이 '/'를 cache-first로 캐싱하는
// 바람에, 한 번 방문한 브라우저는 배포를 해도 옛 화면을 영원히 보게 됐다.
const PRECACHE_URLS = [
    '/static/manifest.json',
    '/static/css/base.css',
    '/static/css/common.css',
    '/static/css/index.css',
    '/static/css/top-stocks.css',
    '/static/js/watchlist.js',
    'https://cdn.jsdelivr.net/npm/bootstrap@5.1.3/dist/css/bootstrap.min.css',
    'https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css',
    'https://cdn.jsdelivr.net/npm/bootstrap@5.1.3/dist/js/bootstrap.bundle.min.js'
];

// 시세·스크리닝 응답. 캐시하면 낡은 가격을 보여주게 되므로 손대지 않는다.
const NETWORK_ONLY = ['/search', '/filter', '/ncav', '/watchlist', '/sitemap.xml', '/robots.txt'];

self.addEventListener('install', event => {
    event.waitUntil(
        caches.open(CACHE_NAME)
            // 한 URL이 실패해도 설치 전체가 실패하지 않도록 개별 처리한다.
            // CDN이 잠깐 죽으면 서비스 워커가 아예 설치되지 않던 문제 방지.
            .then(cache => Promise.allSettled(PRECACHE_URLS.map(u => cache.add(u))))
            .then(() => self.skipWaiting())
    );
});

self.addEventListener('activate', event => {
    event.waitUntil(
        caches.keys()
            .then(keys => Promise.all(
                keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k))
            ))
            .then(() => self.clients.claim())
    );
});

self.addEventListener('fetch', event => {
    const req = event.request;
    if (req.method !== 'GET') return;

    let url;
    try {
        url = new URL(req.url);
    } catch (e) {
        return;
    }
    const sameOrigin = url.origin === self.location.origin;

    // 1) API 응답은 서비스 워커가 관여하지 않는다 (항상 네트워크)
    if (sameOrigin && NETWORK_ONLY.some(p => url.pathname.startsWith(p))) {
        return;
    }

    // 2) HTML은 network-first.
    //    배포한 변경이 다음 방문에 바로 반영되어야 한다.
    //    네트워크가 죽었을 때만 캐시된 사본으로 대체한다.
    const accept = req.headers.get('accept') || '';
    if (req.mode === 'navigate' || accept.includes('text/html')) {
        event.respondWith(
            fetch(req)
                .then(res => {
                    if (res.ok) {
                        const copy = res.clone();
                        event.waitUntil(caches.open(CACHE_NAME).then(c => c.put(req, copy)));
                    }
                    return res;
                })
                .catch(() => caches.match(req).then(r => r || caches.match('/')))
        );
        return;
    }

    // 3) 같은 출처 정적 자원은 stale-while-revalidate.
    //    캐시를 즉시 주되 뒤에서 갱신하므로, CSS/JS를 고쳐도 캐시 버전을
    //    손으로 올릴 필요 없이 다음 방문에 반영된다.
    if (sameOrigin) {
        event.respondWith(
            caches.match(req).then(cached => {
                const network = fetch(req).then(res => {
                    if (res.ok) {
                        const copy = res.clone();
                        event.waitUntil(caches.open(CACHE_NAME).then(c => c.put(req, copy)));
                    }
                    return res;
                });
                if (cached) {
                    // 캐시를 즉시 응답하고 갱신은 뒤에서 진행한다.
                    // 갱신이 실패해도 사용자는 캐시본을 이미 받았으므로 삼킨다.
                    event.waitUntil(network.catch(() => {}));
                    return cached;
                }
                // 캐시가 없으면 네트워크 결과를 그대로 쓴다. 실패하면 실패가
                // 그대로 전달되어야 한다(undefined를 반환하면 안 된다).
                return network;
            })
        );
        return;
    }

    // 4) CDN은 URL에 버전이 박혀 있으므로 cache-first로 충분하다.
    event.respondWith(
        caches.match(req).then(cached => {
            if (cached) return cached;
            return fetch(req).then(res => {
                const copy = res.clone();
                event.waitUntil(caches.open(CACHE_NAME).then(c => c.put(req, copy)));
                return res;
            });
        })
    );
});
