/** 假的 chrome 扩展环境（只有后台会碰到的那部分 API）+ 一个最小的 IndexedDB，
 *  让真正的后台模块（build/background/service-worker.js）在 Node 里跑。
 *
 * 用法：
 *   const env = installFakeChrome({ fetch: srv.fetch });
 *   await env.startWorker();              // 加载（或「重启」）后台：清掉旧监听器、重新 import 一个新实例
 *   env.addTab(3, "https://www.amazon.com/gp/css/order-history", (msg) => ({ ... }));  // 内容脚本的答复
 *   await env.fromTab(3, { type: "pc.hello", ... });   // 内容脚本发给后台的消息（sender.tab 有值）
 *   await env.fromPage({ type: "pc.saveSettings", ... }); // 扩展自己的页面发给后台的消息（sender.url 在扩展源下）
 *   await env.fireAlarm("pc.runBeat");
 *   await env.settle();                   // 等后台的后续动作（void 掉的异步）跑完
 *
 * storage.local / storage.session 在「重启」之间保留（session 只有 clearSession() 才清，模拟插件更新）。
 */

let workerSeq = 0;

class Event {
  constructor() {
    this.listeners = [];
  }
  addListener(fn) {
    this.listeners.push(fn);
  }
  removeListener(fn) {
    this.listeners = this.listeners.filter((f) => f !== fn);
  }
  reset() {
    this.listeners = [];
  }
}

function area() {
  let data = {};
  return {
    async get(key) {
      if (key === null || key === undefined) return structuredClone(data);
      const keys = Array.isArray(key) ? key : [key];
      const out = {};
      for (const k of keys) if (k in data) out[k] = structuredClone(data[k]);
      return out;
    },
    async set(obj) {
      for (const [k, v] of Object.entries(obj)) data[k] = structuredClone(v);
    },
    async remove(keys) {
      for (const k of Array.isArray(keys) ? keys : [keys]) delete data[k];
    },
    _all: () => data,
    _clear: () => {
      data = {};
    },
  };
}

/** 最小的 IndexedDB：open（含 onupgradeneeded）、objectStoreNames.contains、createObjectStore、
 *  transaction(store, mode).objectStore(store).get/put/delete，回调都异步触发。 */
function fakeIndexedDB() {
  const dbs = new Map();
  const later = (fn) => setImmediate(fn);
  const request = () => ({ result: undefined, error: null, onsuccess: null, onerror: null, onupgradeneeded: null });
  return {
    _dbs: dbs,
    open(name, version = 1) {
      const req = request();
      later(() => {
        let rec = dbs.get(name);
        const fresh = !rec || rec.version < version;
        if (!rec) {
          rec = { version, stores: new Map() };
          dbs.set(name, rec);
        }
        const db = {
          objectStoreNames: { contains: (s) => rec.stores.has(s) },
          createObjectStore: (s) => {
            rec.stores.set(s, new Map());
          },
          close() {},
          transaction(storeName) {
            const tx = { oncomplete: null, onerror: null, onabort: null, error: null };
            const store = rec.stores.get(storeName);
            if (!store) throw new Error(`no object store ${storeName}`);
            let pending = 0;
            const done = () => {
              if (--pending === 0) later(() => tx.oncomplete?.());
            };
            const op = (fn) => {
              const r = request();
              pending++;
              later(() => {
                r.result = fn();
                r.onsuccess?.();
                done();
              });
              return r;
            };
            tx.objectStore = () => ({
              get: (k) => op(() => structuredClone(store.get(k))),
              put: (v, k) => op(() => (store.set(k, structuredClone(v)), k)),
              delete: (k) => op(() => (store.delete(k), undefined)),
            });
            return tx;
          },
        };
        req.result = db;
        if (fresh) {
          rec.version = version;
          req.onupgradeneeded?.();
        }
        req.onsuccess?.();
      });
      return req;
    },
  };
}

/** 输入：{fetch, manifestVersion} → 输出：测试驱动用的环境对象；同时把 chrome、fetch、indexedDB 装到 globalThis。 */
export const EXT_ROOT = "chrome-extension://pcdemoextensionid0000000000000000/";
export const EXT_ORIGIN = EXT_ROOT.replace(/\/$/, "");

export function installFakeChrome({ fetch, manifestVersion = "0.2.0" } = {}) {
  const local = area();
  const session = area();
  const alarms = new Map();
  const tabs = new Map(); // id → {id, url, active, lastAccessed, discarded, handler}
  const events = {
    message: new Event(),
    installed: new Event(),
    startup: new Event(),
    alarm: new Event(),
    tabRemoved: new Event(),
  };
  const sentToTabs = [];
  const createdTabs = [];
  let openedOptions = 0;

  const chrome = {
    runtime: {
      id: "pcdemoextensionid0000000000000000",
      getURL: (path = "") => EXT_ROOT + String(path).replace(/^\//, ""),
      getManifest: () => ({ version: manifestVersion }),
      onMessage: events.message,
      onInstalled: events.installed,
      onStartup: events.startup,
      openOptionsPage: async () => {
        openedOptions++;
      },
    },
    storage: { local, session },
    alarms: {
      async get(name) {
        return alarms.has(name) ? { ...alarms.get(name) } : undefined;
      },
      async getAll() {
        return [...alarms.values()].map((a) => ({ ...a }));
      },
      async create(name, info) {
        const when = info.when ?? Date.now() + (info.delayInMinutes ?? info.periodInMinutes ?? 0) * 60_000;
        alarms.set(name, { name, scheduledTime: when, periodInMinutes: info.periodInMinutes });
      },
      async clear(name) {
        return alarms.delete(name);
      },
      onAlarm: events.alarm,
    },
    tabs: {
      async get(id) {
        const t = tabs.get(id);
        if (!t) throw new Error(`No tab with id: ${id}.`);
        return { id: t.id, active: t.active, discarded: t.discarded, lastAccessed: t.lastAccessed };
      },
      async query({ url } = {}) {
        const re = url ? new RegExp("^" + url.replace(/[.?+^$()[\]{}|\\]/g, "\\$&").replace(/\*/g, ".*") + "$") : null;
        return [...tabs.values()]
          .filter((t) => !re || re.test(t.url))
          .map((t) => ({ id: t.id, active: t.active, discarded: t.discarded, lastAccessed: t.lastAccessed }));
      },
      /** 不需要 tabs 权限：只记下开了哪个地址。 */
      async create(info) {
        createdTabs.push(info?.url ?? null);
        return { id: 9000 + createdTabs.length };
      },
      async sendMessage(id, msg) {
        sentToTabs.push({ tabId: id, msg });
        const t = tabs.get(id);
        if (!t || !t.handler) throw new Error("Could not establish connection. Receiving end does not exist.");
        return await t.handler(msg);
      },
      onRemoved: events.tabRemoved,
    },
  };

  globalThis.chrome = chrome;
  globalThis.indexedDB = fakeIndexedDB();
  if (fetch) globalThis.fetch = fetch;

  const settle = async (rounds = 60) => {
    for (let i = 0; i < rounds; i++) await new Promise((r) => setImmediate(r));
  };

  return {
    chrome,
    local,
    session,
    alarms,
    tabs,
    sentToTabs,
    createdTabs,
    get openedOptions() {
      return openedOptions;
    },
    settle,
    /** 加载（或「重启」）后台：旧实例的监听器全部作废（MV3 后台被回收），import 一个新实例。 */
    async startWorker() {
      for (const e of Object.values(events)) e.reset();
      await import(`../build/background/service-worker.js?worker=${++workerSeq}`);
      await settle();
    },
    /** 插件更新、重新加载：storage.session 清空。 */
    clearSession() {
      session._clear();
    },
    async fireInstalled(reason = "update") {
      for (const fn of events.installed.listeners) fn({ reason });
      await settle();
    },
    async fireStartup() {
      for (const fn of events.startup.listeners) fn();
      await settle();
    },
    async fireAlarm(name) {
      for (const fn of events.alarm.listeners) fn({ name, scheduledTime: Date.now() });
      await settle();
    },
    addTab(id, url, handler = null, extra = {}) {
      tabs.set(id, { id, url, active: false, lastAccessed: Date.now(), discarded: false, handler, ...extra });
    },
    setTabHandler(id, handler) {
      tabs.get(id).handler = handler;
    },
    async removeTab(id) {
      tabs.delete(id);
      for (const fn of events.tabRemoved.listeners) fn(id, { windowId: 1, isWindowClosing: false });
      await settle();
    },
    /** 内容脚本（标签页 tabId）或扩展页面（tabId 为 null）发给后台的一条消息；返回后台的答复。
     *  sender 照浏览器的样子给：内容脚本带 tab、所在网页的地址与源；扩展页面带扩展源下的地址与源，
     *  没开在标签页里的（弹出页）没有 tab。第三个参数可以整个替换 sender（测特权消息的来源检查）。 */
    send(tabId, msg, senderOverride = null) {
      return new Promise((resolve, reject) => {
        const pageUrl = tabId === null ? null : tabs.get(tabId)?.url ?? "https://www.amazon.com/";
        const sender = senderOverride ?? (tabId === null
          ? { id: chrome.runtime.id, url: EXT_ROOT + "popup.html", origin: EXT_ORIGIN }
          : { id: chrome.runtime.id, tab: { id: tabId }, url: pageUrl, origin: new URL(pageUrl).origin, frameId: 0 });
        let answered = false;
        const respond = (v) => {
          answered = true;
          resolve(v);
        };
        const ls = events.message.listeners;
        if (!ls.length) return reject(new Error("Could not establish connection. Receiving end does not exist."));
        for (const fn of ls) {
          const keep = fn(msg, sender, respond);
          if (keep !== true && !answered) resolve(undefined);
        }
      });
    },
    fromTab(tabId, msg) {
      return this.send(tabId, msg);
    },
    /** 扩展自己的、没开在标签页里的页面（弹出页）。 */
    fromPage(msg) {
      return this.send(null, msg);
    },
    /** 设置页：清单里 options_ui.open_in_tab = true，它是开在标签页里的扩展页面，消息带着 sender.tab。 */
    fromOptionsTab(msg, tabId = 900) {
      return this.send(null, msg, { id: chrome.runtime.id, tab: { id: tabId }, url: EXT_ROOT + "options.html", origin: EXT_ORIGIN, frameId: 0 });
    },
    logs() {
      return (local._all()["pc.logs"] ?? []).map((l) => l.text);
    },
  };
}
