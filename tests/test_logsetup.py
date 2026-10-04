"""日志落盘回归（§6 v0.42）：路径推导与 dictConfig 生效落盘。"""

from __future__ import annotations

import logging
import logging.config

from kbserver.logsetup import LOG_FILENAME, build_log_config, log_file_for


def test_log_file_follows_config_dir(tmp_path):
    """日志位置随 kbserver.config.json 所在目录，与进程 cwd 无关。"""
    cfg = tmp_path / "kbserver.config.json"
    expected = cfg.resolve().parent / "logs" / LOG_FILENAME
    assert log_file_for(cfg) == expected


def test_build_log_config_applies_and_writes(tmp_path):
    """dictConfig 应用后日志写入 logs/kbserver.log；构建配置时即建目录。"""
    cfg = tmp_path / "sub" / "kbserver.config.json"
    conf = build_log_config(cfg)
    # 目录在构建配置时即创建（uvicorn 启动前），不依赖 handler 惰性建目录
    assert (cfg.parent / "logs").is_dir()

    logging.config.dictConfig(conf)
    try:
        logging.getLogger("test.logsetup").warning("落盘冒烟")
        log_file = cfg.parent / "logs" / LOG_FILENAME
        content = log_file.read_text(encoding="utf-8")
        assert "落盘冒烟" in content
        assert "test.logsetup" in content
        # uvicorn 三 logger 均透传 root，不自带 handler
        for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
            logger_conf = conf["loggers"][name]
            assert logger_conf["propagate"] is True
            assert "handlers" not in logger_conf or not logger_conf["handlers"]
    finally:
        # 清理 root handler 并关文件句柄，避免占用 tmp_path（Windows 句柄锁）
        root = logging.getLogger()
        for h in list(root.handlers):
            root.removeHandler(h)
            h.close()
