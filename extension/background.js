// kb buddy clipper - MV3 service worker
// 职责：入口层侦察兵——只投递，不理解内容、不做格式转换（方案文档 §11.4）。
// 服务不可达时本地暂存（chrome.storage.local 队列），恢复后经 alarms/startup 自动重发（§8 单点风险对冲）。
// 反馈：角标 + 可选系统通知；投递受理后跟踪管道结果，入库/提取失败均有通知（可选权限）。

const DEFAULT_SERVER = "http://127.0.0.1:8765";
const QUEUE_KEY = "queue";
const PENDING_KEY = "pending_checks";
const MAX_CHECKS = 3;

async function getConfig() {
  const { server, token } = await chrome.storage.local.get(["server", "token"]);
  return { server: (server || DEFAULT_SERVER).replace(/\/+$/, ""), token: token || "" };
}

function badge(text, color) {
  try {
    chrome.action.setBadgeText({ text });
    chrome.action.setBadgeBackgroundColor({ color });
    setTimeout(() => chrome.action.setBadgeText({ text: "" }), 5000);
  } catch (e) {
    /* badge 不可用时静默 */
  }
}

async function notify(title, message) {
  try {
    const granted = await chrome.permissions.contains({ permissions: ["notifications"] });
    if (!granted) return;
    chrome.notifications.create({
      type: "basic",
      iconUrl: "icon128.png",
      title,
      message: message || "",
    });
  } catch (e) {
    /* 未授权或通知不可用时静默 */
  }
}

async function post(server, token, payload) {
  const resp = await fetch(server + "/api/capture", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...(token ? { "X-KB-Token": token } : {}),
    },
    body: JSON.stringify(payload),
  });
  if (!resp.ok) throw new Error("HTTP " + resp.status);
  return resp.json();
}

async function enqueue(payload) {
  const { [QUEUE_KEY]: queue = [] } = await chrome.storage.local.get(QUEUE_KEY);
  queue.push({ payload, at: Date.now() });
  await chrome.storage.local.set({ [QUEUE_KEY]: queue });
}

async function flushQueue() {
  const { [QUEUE_KEY]: queue = [] } = await chrome.storage.local.get(QUEUE_KEY);
  if (!queue.length) return;
  const { server, token } = await getConfig();
  const rest = [];
  for (const item of queue) {
    try {
      await post(server, token, item.payload);
    } catch (e) {
      rest.push(item);
    }
  }
  await chrome.storage.local.set({ [QUEUE_KEY]: rest });
  if (rest.length === 0) badge("OK", "#16a34a");
}

// 管道结果跟踪：按 entry_id 或"host+路径"匹配入库条目（站点重定向会漂移 id，路径匹配兜底）；
// 命中 error 列表 → 通知失败原因；连续 MAX_CHECKS 次皆无 → 放弃等待。
function samePath(a, b) {
  try {
    const ua = new URL(a);
    const ub = new URL(b);
    return ua.host === ub.host && ua.pathname === ub.pathname;
  } catch (e) {
    return false;
  }
}

async function checkPending() {
  const { [PENDING_KEY]: pending = {} } = await chrome.storage.local.get(PENDING_KEY);
  const ids = Object.keys(pending);
  if (!ids.length) return;
  const { server, token } = await getConfig();
  const headers = token ? { "X-KB-Token": token } : {};
  let entries = [];
  let errors = [];
  try {
    const [entriesResp, statusResp] = await Promise.all([
      fetch(`${server}/api/entries`, { headers }),
      fetch(`${server}/api/status`, { headers }),
    ]);
    if (!entriesResp.ok || !statusResp.ok) return; // 下个 alarm 再查
    entries = (await entriesResp.json()).entries || [];
    errors = (await statusResp.json()).errors || [];
  } catch (e) {
    return; // 服务不可达，下个 alarm 再查
  }
  const keep = {};
  for (const id of ids) {
    const item = pending[id];
    const hit = entries.some((e) => e.id === id || (item.url && samePath(e.url, item.url)));
    if (hit) {
      await notify("已入库 kb", item.title || id);
      continue;
    }
    const err = errors.find((e) => e.id === id);
    if (err) {
      await notify("该页面提取失败", "试试选中一段文字再剪藏；详情见服务 status");
      continue;
    }
    item.attempts = (item.attempts || 0) + 1;
    if (item.attempts < MAX_CHECKS) keep[id] = item;
  }
  await chrome.storage.local.set({ [PENDING_KEY]: keep });
}

async function track(entryId, title, url) {
  const { [PENDING_KEY]: pending = {} } = await chrome.storage.local.get(PENDING_KEY);
  pending[entryId] = { title, url, attempts: 0 };
  await chrome.storage.local.set({ [PENDING_KEY]: pending });
}

async function clipCurrentTab(info) {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab || !tab.url) return;

  let selection = "";
  try {
    const [res] = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: () => String(window.getSelection() || ""),
    });
    selection = (res && res.result) || "";
  } catch (e) {
    /* 受限页面（chrome:// 等）无法注入，忽略 */
  }

  let screenshot = null;
  try {
    const dataUrl = await chrome.tabs.captureVisibleTab(tab.windowId, { format: "png" });
    screenshot = dataUrl.split(",")[1] || null;
  } catch (e) {
    /* 受限页面无法截图，忽略 */
  }

  const payload = {
    url: (info && info.linkUrl) || tab.url,
    title: tab.title || "",
    text: (info && info.selectionText) || selection || undefined,
    screenshot_b64: screenshot,
    entry: "browser_ext",
  };

  const { server, token } = await getConfig();
  const selectionNote = payload.text ? `（选中文本 ${payload.text.length} 字）` : "（未检测到选中文本）";
  try {
    const result = await post(server, token, payload);
    badge("OK", "#16a34a");
    if (result && result.entry_id) await track(result.entry_id, payload.title, payload.url);
    await notify("已存入 kb", (payload.title || payload.url || "") + selectionNote);
    flushQueue();
  } catch (e) {
    await enqueue(payload);
    badge("!", "#dc2626");
    await notify("服务不可达，已离线暂存", "服务恢复后每分钟自动重发");
  }
}

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({
    id: "kb-clip",
    title: "存入 kb buddy",
    contexts: ["page", "selection", "link", "image"],
  });
  chrome.alarms.create("flush", { periodInMinutes: 1 });
});

chrome.contextMenus.onClicked.addListener((info) => clipCurrentTab(info));
chrome.commands.onCommand.addListener(() => clipCurrentTab());
chrome.runtime.onStartup.addListener(() => flushQueue());
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === "flush") {
    flushQueue();
    checkPending();
  }
});
chrome.runtime.onMessage.addListener((msg) => {
  if (msg && msg.type === "flush") {
    flushQueue();
    checkPending();
  }
  if (msg && msg.type === "test-notify") {
    notify("通知测试", "如果你看到这条，完成通知已生效");
  }
});
