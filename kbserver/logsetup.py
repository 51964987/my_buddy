"""服务日志落盘（§6 v0.42）：uvicorn 统一日志配置。

日志属运维产物，不是 kb/ 业务数据，不经过写边界守卫；
固定落在配置文件同目录的 logs/kbserver.log（RotatingFileHandler 滚动），
后台/隐藏窗口启动时 stdout 不再是日志唯一出口，排障有据可查。
"""

from __future__ import annotations

from pathlib import Path

LOG_FILENAME = "kbserver.log"
LOG_MAX_BYTES = 5 * 1024 * 1024  # 单文件上限，超限滚动出 .1/.2/.3
LOG_BACKUP_COUNT = 3
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def log_file_for(config_path: Path) -> Path:
    """日志文件路径 = 配置文件（kbserver.config.json）同目录的 logs/kbserver.log。

    与配置同源而非进程 cwd：无论从哪个目录启动，位置都可预期。
    """
    return Path(config_path).resolve().parent / "logs" / LOG_FILENAME


def build_log_config(config_path: Path) -> dict:
    """构建 uvicorn.run(log_config=...) 的 dictConfig。

    uvicorn/uvicorn.error/uvicorn.access 与 kbserver.* 全部 propagate 到 root，
    root 同时挂控制台与滚动文件 handler——控制台与文件内容一致，不双份。
    构建时即创建 logs/ 目录，避免启动阶段目录缺失导致文件 handler 报错。
    """
    log_file = log_file_for(config_path)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {"format": LOG_FORMAT},
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "formatter": "default",
                "stream": "ext://sys.stderr",
            },
            "file": {
                "class": "logging.handlers.RotatingFileHandler",
                "formatter": "default",
                "filename": str(log_file),
                "maxBytes": LOG_MAX_BYTES,
                "backupCount": LOG_BACKUP_COUNT,
                "encoding": "utf-8",
            },
        },
        "root": {"level": "INFO", "handlers": ["console", "file"]},
        "loggers": {
            "uvicorn": {"level": "INFO", "propagate": True},
            "uvicorn.error": {"level": "INFO", "propagate": True},
            "uvicorn.access": {"level": "INFO", "propagate": True},
        },
    }
