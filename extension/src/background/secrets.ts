/** 设备令牌的存放处：扩展自己源（chrome-extension://<id>）下的 IndexedDB。
 *
 * 令牌由插件自己生成（core/register.generateDeviceToken），只是会话标识：把批次、尝试、回执归到同一个浏览器。
 * 和它一起存的还有**它所属的服务端地址**：令牌只发往这个地址。改服务端地址时令牌与地址在同一个事务里一起换
 * （旧令牌绝不发往新地址）；chrome.storage.local 里的设置被改了而这里没换，后台仍按这里的地址说话。
 *
 * 为什么不放 chrome.storage.local：MV3 里 storage.local 对内容脚本是开放的（只有 storage.session
 * 默认限于受信任的上下文，而 session 在浏览器重启后清空，放那里每次重启都会变成一个新的浏览器）。
 * 内容脚本跑在亚马逊页面的源下，它的 indexedDB 是 amazon.com 的，打不开扩展源下的这个库；
 * 只有后台（以及设置页、弹出页这些扩展页面）能打开 —— 而只有后台会打开。
 *
 * 读写失败不抛，但「读不了」与「没有令牌」分开说：库里确实没有令牌时后台才生成一个新的；
 * 库读不了、或新令牌写不进去时后台**不登记**（否则每次唤醒都带着一个新令牌去登记，在服务端刷出一串浏览器；
 * 读不了的时候更不能拿一个新令牌把库里原有的那个盖掉），界面上写明原因。
 * 同一个后台实例里缓存一份，免得每个请求都开一次库（缓存认库：换了一个 indexedDB 对象就作废）。
 */

const DB_NAME = "pc-secrets";
const STORE = "kv";
const TOKEN_KEY = "device_token";
const URL_KEY = "device_token_url";

/** 令牌与它所属的服务端地址。url 为 null：旧版留下的令牌，还没有记过地址（后台按旧的登记记录补上）。 */
export interface Secret {
  token: string;
  url: string | null;
}

let cache: { db: IDBFactory; secret: Secret | null } | null = null;

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

/** 一个事务里做几件事；全部完成（事务提交）才算成功。返回各请求的结果。 */
async function run(mode: IDBTransactionMode, fn: (s: IDBObjectStore) => IDBRequest[]): Promise<unknown[]> {
  const db = await openDb();
  try {
    return await new Promise<unknown[]>((resolve, reject) => {
      const tx = db.transaction(STORE, mode);
      const reqs = fn(tx.objectStore(STORE));
      const out: unknown[] = new Array(reqs.length).fill(undefined);
      reqs.forEach((req, i) => {
        req.onsuccess = () => {
          out[i] = req.result;
        };
      });
      tx.oncomplete = () => resolve(out);
      tx.onerror = () => reject(tx.error ?? new Error("插件密钥库读写失败"));
      tx.onabort = () => reject(tx.error ?? new Error("插件密钥库读写被中止"));
    });
  } finally {
    db.close();
  }
}

/** 读密钥库的结论：读到了（secret 为 null = 库里确实没有令牌），或读不了。 */
export type SecretRead = { ok: true; secret: Secret | null } | { ok: false };

/** 输入：无 → 输出：令牌与它所属的地址（库里没有为 secret: null）；库读不了为 ok: false。 */
export async function readSecret(): Promise<SecretRead> {
  if (cache && cache.db === indexedDB) return { ok: true, secret: cache.secret };
  try {
    const [t, u] = await run("readonly", (s) => [s.get(TOKEN_KEY), s.get(URL_KEY)]);
    const secret = typeof t === "string" && t !== "" ? { token: t, url: typeof u === "string" && u !== "" ? u : null } : null;
    cache = { db: indexedDB, secret };
    return { ok: true, secret };
  } catch {
    return { ok: false }; // 不缓存失败：下一次再试
  }
}

/** 输入：令牌、它所属的服务端地址 → 输出：是否写入成功（两项在同一个事务里，要么都换、要么都不换）。 */
export async function writeSecret(token: string, url: string): Promise<boolean> {
  try {
    await run("readwrite", (s) => [s.put(token, TOKEN_KEY), s.put(url, URL_KEY)]);
    cache = { db: indexedDB, secret: { token, url } };
    return true;
  } catch {
    cache = null;
    return false;
  }
}
