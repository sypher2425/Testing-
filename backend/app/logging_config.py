"""Structured JSON logging. Every log line includes job_id when available."""
import logging
import sys

from pythonjsonlogger import jsonlogger


class _DefaultJobIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "job_id"):
            record.job_id = "-"
        return True


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(_DefaultJobIdFilter())
    formatter = jsonlogger.JsonFormatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s %(job_id)s"
    )
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)


def get_job_logger(job_id: str) -> logging.LoggerAdapter:
    logger = logging.getLogger("pipeline")
    return logging.LoggerAdapter(logger, {"job_id": job_id})
