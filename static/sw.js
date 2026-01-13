const CACHE_NAME = 'partner-sms-v1';
const urlsToCache = [
  '/',
  '/contacts',
  '/inbox',
  '/broadcast',
  '/sequences',
  '/static/manifest.json'
];

// Install event - cache resources
self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then(cache => {
        console.log('Opened cache');
        return cache.addAll(urlsToCache);
      })
      .catch(err => {
        console.log('Cache failed:', err);
      })
  );
  self.skipWaiting();
});

// Activate event - clean old caches
self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(cacheNames => {
      return Promise.all(
        cacheNames.map(cacheName => {
          if (cacheName !== CACHE_NAME) {
            return caches.delete(cacheName);
          }
        })
      );
    })
  );
  self.clients.claim();
});

// Fetch event - network first, fall back to cache
self.addEventListener('fetch', event => {
  // Skip non-GET requests
  if (event.request.method !== 'GET') {
    return;
  }
  
  // Skip API requests - always fetch fresh
  if (event.request.url.includes('/api/')) {
    return;
  }
  
  event.respondWith(
    fetch(event.request)
      .then(response => {
        // Clone response for caching
        const responseClone = response.clone();
        caches.open(CACHE_NAME)
          .then(cache => {
            cache.put(event.request, responseClone);
          });
        return response;
      })
      .catch(() => {
        // Network failed, try cache
        return caches.match(event.request);
      })
  );
});

// Push notification handler
self.addEventListener('push', event => {
  const options = {
    body: event.data ? event.data.text() : 'New message received',
    icon: '/static/icon-192.png',
    badge: '/static/icon-192.png',
    vibrate: [100, 50, 100],
    data: {
      url: '/inbox'
    }
  };
  
  event.waitUntil(
    self.registration.showNotification('Partner SMS', options)
  );
});

// Notification click handler
self.addEventListener('notificationclick', event => {
  event.notification.close();
  event.waitUntil(
    clients.openWindow(event.notification.data.url || '/')
  );
});
