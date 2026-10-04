"""运行期依赖自检（启动契约，v0.36）。

背景（v0.35 末实跑踩坑）：`jieba` / `sqlite-vec` / `playwright` 在代码里都是
**延迟导入**（省启动耗时：jieba 词典加载约 1 秒、playwright 只在动态页兜底时用），
缺依赖不会在启动时暴露，只在用户点某条路径时变成无信息 500 —— 实测「检索」页
输入任意词稳定 500，根因 `ModuleNotFoundError: No module named 'jieba'`，而
`/api/index/status` 不触发分词故返回 200，排查方向一度被带偏。

口径（与「失败要可观测」纪律一致，宁可不启动也不提供半可用服务）：
- **必需依赖缺失** → 启动即失败，错误信息给出当前解释器的安装命令；
- **可选依赖缺失** → 仅告警，对应功能走各自既有降级口径（语义检索 409、
  动态页兜底抛带安装提示的 `RuntimeError`），既不静默也不拖垮其它功能。

存在性用 `importlib.util.find_spec` 而非真导入：只探测不执行模块代码，
不给启动白白加上 jieba 词典加载与 playwright 导入开销。
"""

from __future__ import annotations

import importlib.util
import logging
import sys

logger = logging.getLogger(__name__)

# 必需依赖：(import 名, pip 包名, 缺失影响)。缺任一项 → 服务无法提供声明功能
REQUIRED_DEPENDENCIES: tuple[tuple[str, str, str], ...] = (
    ("fastapi", "fastapi", "API 服务"),
    ("uvicorn", "uvicorn", "API 服务与静态托管"),
    ("pydantic", "pydantic", "请求体校验"),
    ("httpx", "httpx", "页面抓取与 LLM 调用"),
    ("yaml", "PyYAML", "frontmatter 解析（写边界守卫依赖）"),
    ("bs4", "beautifulsoup4", "HTML 解析"),
    ("trafilatura", "trafilatura", "正文提取"),
    ("markdownify", "markdownify", "HTML 转 Markdown"),
    ("jieba", "jieba", "中文分词（全文检索与索引构建）"),
)

# 可选依赖：(import 名, pip 包名, 缺失影响)。缺任一项 → 只影响对应可选功能
OPTIONAL_DEPENDENCIES: tuple[tuple[str, str, str], ...] = (
    ("sqlite_vec", "sqlite-vec", "语义检索（需配置 index.vector_enabled=true）"),
    ("playwright", "playwright", "动态页兜底抓取（另需 playwright install chromium）"),
)


class MissingDependencyError(RuntimeError):
    """必需依赖缺失：启动期直接失败（错误信息含安装命令与受影响功能）。"""

    def __init__(self, missing: list[tuple[str, str, str]]) -> None:
        self.missing = list(missing)
        detail = "、".join(f"{pkg}（影响：{impact}）" for _mod, pkg, impact in self.missing)
        super().__init__(
            f"缺少必需依赖：{detail}。"
            f"请用当前解释器安装：\"{sys.executable}\" -m pip install -r requirements.txt"
        )


def missing_dependencies(
    spec: tuple[tuple[str, str, str], ...],
) -> list[tuple[str, str, str]]:
    """返回 spec 中当前解释器里找不到的依赖项（只探测存在性，不执行模块）。"""
    missing: list[tuple[str, str, str]] = []
    for mod, pkg, impact in spec:
        try:
            found = importlib.util.find_spec(mod) is not None
        except (ImportError, ValueError):
            found = False  # 命名空间包等异常形态按缺失处理，不因探测失败阻断启动
        if not found:
            missing.append((mod, pkg, impact))
    return missing


def check_dependencies() -> dict[str, list[tuple[str, str, str]]]:
    """服务启动自检：必需缺失抛 `MissingDependencyError`，可选缺失告警。

    返回缺失清单（可选依赖项；必需缺失时不会返回而是抛异常），供测试断言。
    """
    missing_required = missing_dependencies(REQUIRED_DEPENDENCIES)
    if missing_required:
        raise MissingDependencyError(missing_required)
    missing_optional = missing_dependencies(OPTIONAL_DEPENDENCIES)
    if missing_optional:
        detail = "、".join(f"{pkg}（影响：{impact}）" for _mod, pkg, impact in missing_optional)
        logger.warning("缺少可选依赖，相关功能不可用：%s", detail)
    return {"missing_required": [], "missing_optional": missing_optional}
