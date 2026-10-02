// kb buddy clipper - MV3 service worker
// 职责：入口层侦察兵——只投递，不理解内容、不做格式转换（方案文档 §11.4）。
// 服务不可达时本地暂存（chrome.storage.local 队列），恢复后经 alarms/startup 自动重发（§8 单点风险对冲）。

const DEFAULT_SERVER = "http://127.0.0.1:8765";
const QUEUE_KEY = "queue";

async function getConfig() {
  const { server, token } = await chrome.storage.local.get(["server", "token"]);
  return { server: (server || DEFAULT_SERVER).replace(/\/+$/, ""), token: token || "" };
}

function badge(text, color) {
  try {
    chrome.action.setBadgeText({ text });
    chrome.action.setBadgeBackgroundColor({ color });
    setTimeout(() => chrome.action.setBadgeText({ text: "" }), 2500);
  } catch (e) {
    /* badge 不可用时静默 */
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
  if (!queue.length) return 0;
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
  return queue.length - rest.length;
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
    title: (info && info.selectionText ? "" : tab.title) || tab.title || "",
    text: (info && info.selectionText) || selection || undefined,
    screenshot_b64: screenshot,
    entry: "browser_ext",
  };

  const { server, token } = await getConfig();
  try {
    await post(server, token, payload);
    badge("OK", "#16a34a");
    flushQueue();
  } catch (e) {
    await enqueue(payload);
    badge("!", "#dc2626");
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
  if (alarm.name === "flush") flushQueue();
});
chrome.runtime.onMessage.addListener((msg) => {
  if (msg && msg.type === "flush") flushQueue();
});
