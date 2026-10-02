const $ = (id) => document.getElementById(id);

async function refresh() {
  const { server, token, queue = [] } = await chrome.storage.local.get(["server", "token", "queue"]);
  $("server").value = server || "http://127.0.0.1:8765";
  $("token").value = token || "";
  $("queue").textContent = "离线暂存条数：" + queue.length;
}

$("save").addEventListener("click", async () => {
  await chrome.storage.local.set({
    server: $("server").value.trim() || "http://127.0.0.1:8765",
    token: $("token").value.trim(),
  });
  $("saved").textContent = "已保存";
  setTimeout(() => ($("saved").textContent = ""), 2000);
  refresh();
});

$("flush").addEventListener("click", async () => {
  chrome.runtime.sendMessage({ type: "flush" });
  setTimeout(refresh, 1500);
});

refresh();
