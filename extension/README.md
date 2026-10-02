# kb buddy clipper（MV3 浏览器扩展 · P1）

右键 / 快捷键把当前页剪藏到 kbserver：URL + 标题 + 选中文本 + 页面截图。
服务不可达时本地暂存（chrome.storage.local），恢复后每分钟自动重发。

## 安装（Chrome / Edge 通用）

1. 打开 `chrome://extensions`（Edge 为 `edge://extensions`）
2. 开启「开发者模式」→「加载已解压的扩展程序」→ 选择本 `extension/` 目录
3. 扩展图标右键 →「选项」：填写服务地址与 token（与服务端 `kbserver.config.json` 一致）

## 使用

- 右键菜单「存入 kb buddy」：页面 / 选中文本 / 链接 / 图片 均可
- 快捷键 `Alt+Shift+S` 剪藏当前页
- 工具栏角标：`OK` 绿色=投递成功；`!` 红色=服务不可达已离线暂存

## 权限说明（对齐方案文档 §9 决策 3：功能最小化）

- `activeTab` + `scripting`：仅在你主动剪藏的页面上读取选中文本与截图
- `contextMenus` / `storage` / `alarms`：右键菜单、配置与离线队列（不涉及任何站点数据）
- `host_permissions` 仅限本机 `localhost` / `127.0.0.1`；若服务经局域网/Tailscale 访问，把对应地址加入 `host_permissions` 后重新加载扩展

## 文件结构

```
extension/
├── manifest.json    # MV3 清单
├── background.js    # 投递 + 离线队列 + 重发
├── options.html/js  # 服务地址与 token 配置
```
