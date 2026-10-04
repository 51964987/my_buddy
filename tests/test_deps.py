"""服务启动期依赖自检（v0.36）回归用例。

背景：jieba / sqlite-vec / playwright 在代码里是延迟导入，缺装不在启动时暴露，
只在用户点某条路径时变成无信息 500（实跑踩坑：检索页输入任意词稳定 500）。

覆盖：必需依赖缺失拒绝启动且提示安装命令、可选依赖缺失仅告警、
自检在真实环境通过（防清单与实际依赖漂移）。
"""

import pytest
from fastapi.testclient import TestClient

from kbserver import deps
from kbserver.app import create_app
from kbserver.deps import MissingDependencyError, check_dependencies

FAKE = ("kbserver_no_such_module_xyz", "kbserver-no-such-pkg", "测试用假依赖")


def test_current_env_passes_check():
    """真实环境自检必须通过：清单里的依赖名不能写错（防清单与 requirements 漂移）。"""
    result = check_dependencies()
    assert result["missing_required"] == []


def test_missing_required_dependency_blocks_startup(monkeypatch, cfg, guard):
    """必需依赖缺失 → 启动失败，错误信息带 pip 安装命令与受影响功能。"""
    monkeypatch.setattr(deps, "REQUIRED_DEPENDENCIES", (*deps.REQUIRED_DEPENDENCIES, FAKE))
    with pytest.raises(MissingDependencyError) as ei:
        check_dependencies()
    assert "kbserver-no-such-pkg" in str(ei.value)
    assert "pip install -r requirements.txt" in str(ei.value)
    assert "测试用假依赖" in str(ei.value)

    # 服务入口同样拦住：lifespan 启动阶段抛错，不会进入可用状态
    app = create_app(cfg)
    with pytest.raises(MissingDependencyError):
        with TestClient(app):
            pass


def test_missing_optional_dependency_only_warns(monkeypatch, caplog):
    """可选依赖缺失 → 不阻断启动，仅告警（功能各自走 409 / 安装提示口径）。"""
    monkeypatch.setattr(deps, "OPTIONAL_DEPENDENCIES", (*deps.OPTIONAL_DEPENDENCIES, FAKE))
    with caplog.at_level("WARNING"):
        result = check_dependencies()
    assert [pkg for _m, pkg, _i in result["missing_optional"]] == ["kbserver-no-such-pkg"]
    assert "kbserver-no-such-pkg" in caplog.text
