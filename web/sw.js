// Offline fallback only. While online, always fetch fresh editor modules.
const CACHE="pocket-editor-v2";
const ASSETS=["./","./index.html","./styles.css","./app.mjs","./core.mjs","./manifest.webmanifest","./icon.svg"];
self.addEventListener("install",event=>{
  event.waitUntil(caches.open(CACHE).then(cache=>cache.addAll(ASSETS)).then(()=>self.skipWaiting()));
});
self.addEventListener("activate",event=>{
  event.waitUntil(Promise.all([
    caches.keys().then(keys=>Promise.all(keys.filter(key=>key!==CACHE).map(key=>caches.delete(key)))),
    self.clients.claim()
  ]));
});
self.addEventListener("fetch",event=>{
  const url=new URL(event.request.url);
  if (event.request.method!=="GET" || url.origin!==self.location.origin) return;
  const fallback=event.request.mode==="navigate"?"./index.html":event.request;
  event.respondWith(
    fetch(event.request,{cache:"no-store"}).then(response=>{
      if (response.ok && response.type==="basic") {
        const copy=response.clone();
        event.waitUntil(caches.open(CACHE).then(cache=>cache.put(event.request,copy)));
      }
      return response;
    }).catch(async()=>{
      const saved=await caches.match(fallback);
      if (saved) return saved;
      return Response.error();
    })
  );
});
