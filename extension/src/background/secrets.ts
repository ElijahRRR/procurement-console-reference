/** 设备令牌的存放处：扩展自己源（chrome-extension://<id>）下的 IndexedDB。
 *
 * 为什么不放 chrome.storage.local：MV3 里 storage.local 对内容脚本是开放的（只有 storage.session
 * 默认限于受信任的上下文，而 session 在浏览器重启后清空，放那里每次重启都要重新配对）。
 * 内容脚本跑在亚马逊页面的源下，它的 indexedDB 是 amazon.com 的，打不开扩展源下的这个库；
 * 只有后台（以及设置页、弹出页这些扩展页面）能打开 —— 而只有后台会打开。
 *
 * 读写失败不抛：读不到当成「没有令牌」（界面显示未配对），写不进去由调用方报配对失败。
 * 同一个后台实例里缓存一份，免得每个请求都开一次库。
 */

const DB_NAME = "pc-secrets";
const STORE = "kv";
const TOKEN_KEY = "device_token";

let cache: { token: string | null } | null = null;

function openDb(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, 1);
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains(STORE)) db.createObjectStore(STORE);
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error ?? new Error("打开插件密钥库失败"));
  });
}

async function run<T>(mode: IDBTransactionMode, fn: (s: IDBObjectStore) => IDBRequest): Promise<T> {
  const db = await openDb();
  try {
    return await new Promise<T>((resolve, reject) => {
      const tx = db.transaction(STORE, mode);
      const req = fn(tx.objectStore(STORE));
      let out: T;
      req.onsuccess = () => {
        out = req.result as T;
      };
      tx.oncomplete = () => resolve(out);
      tx.onerror = () => reject(tx.error ?? new Error("插件密钥库读写失败"));
      tx.onabort = () => reject(tx.error ?? new Error("插件密钥库读写被中止"));
    });
  } finally {
    db.close();
  }
}

/** 输入：无 → 输出：设备令牌；没有或读不到为 null。 */
export async function readToken(): Promise<string | null> {
  if (cache) return cache.token;
  try {
    const v = await run<unknown>("readonly", (s) => s.get(TOKEN_KEY));
    const token = typeof v === "string" && v !== "" ? v : null;
    cache = { token };
    return token;
  } catch {
    return null; // 不缓存失败：下一次再试
  }
}

/** 输入：设备令牌 → 输出：是否写入成功。 */
export async function writeToken(token: string): Promise<boolean> {
  try {
    await run("readwrite", (s) => s.put(token, TOKEN_KEY));
    cache = { token };
    return true;
  } catch {
    cache = null;
    return false;
  }
}

/** 输入：无 → 输出：无。删掉设备令牌（解除配对）。 */
export async function deleteToken(): Promise<void> {
  cache = { token: null };
  try {
    await run("readwrite", (s) => s.delete(TOKEN_KEY));
  } catch {
    cache = null;
  }
}
