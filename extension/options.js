const $ = (id) => document.getElementById(id);

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

$("notif").addEventListener("click", async () => {
  const granted = await chrome.permissions.contains({ permissions: ["notifications"] });
  if (granted) {
    await chrome.permissions.remove({ permissions: ["notifications"] });
    refresh();
    return;
  }
  const ok = await chrome.permissions.request({ permissions: ["notifications"] });
  if (!ok) {
    $("notif-state").textContent = "完成通知：授权被拒绝";
    return;
  }
  // 直接在本页创建测试通知，并把失败原因回显到页面（不静默）
  await refresh();
  try {
    const id = await chrome.notifications.create({
      type: "basic",
      iconUrl: "icon128.png",
      title: "通知测试",
      message: "如果你看到这条，完成通知已生效",
    });
    $("notif-state").textContent = "已开启（测试通知已创建 id=" + id + "；若未弹出，查系统通知设置）";
  } catch (e) {
    $("notif-state").textContent = "创建失败 - " + e.message;
  }
});

async function refresh() {
  const { server, token, queue = [] } = await chrome.storage.local.get(["server", "token", "queue"]);
  $("server").value = server || "http://127.0.0.1:8765";
  $("token").value = token || "";
  $("queue").textContent = "离线暂存条数：" + queue.length;
  const granted = await chrome.permissions.contains({ permissions: ["notifications"] });
  $("notif").textContent = granted ? "关闭完成通知" : "开启完成通知";
  $("notif-state").textContent = "完成通知：" + (granted ? "已开启" : "未开启");
}

refresh();
