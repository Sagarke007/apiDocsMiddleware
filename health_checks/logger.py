"""
Utility to setup logger as global variable.
Formatted to list time date level file w lineno in function(), and message.
https://www.loggly.com/ultimate-guide/python-logging-basics/

Good article on loggin in genenral
https://rollbar.com/blog/logging-in-python/

WARNING Using the root logger name=none, or BasicConfig, can affect other loggers.
Python logger advices using __name__ as default, not None
https://docs.python.org/3/library/logging.html#:~:text=The%20logger%20name%20hierarchy%20is,in%20the%20Python%20package%20namespace.
"""

import logging
import os
from pathlib import Path
from tempfile import gettempdir
import requests  # Import requests to send logs to the server

class OneLineLogFormatter(logging.Formatter):
    """Log formatter that converts log text to single line with ¶'s for better reading of log file"""

    def formatException(self, ei):  # ei = exception info
        result = super().formatException(ei)
        if ei.exc_text:
            result = result.replace("\n", " ¶ ")
        return repr(result)

    def format(self, record):
        result = super().format(record)
        if record.exc_text:
            result = result.replace("\n", " ¶ ")
        return result


# 2017-06-06:17:07:02,158 DEBUG    [log.py:11 in func()] This is a debug log
LOG_FORMAT = "%(asctime)s.%(msecs)03d %(levelname)-8s [%(filename)s:%(lineno)d in %(funcName)s()] %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d:%H:%M:%S"


class CentralizedServerHandler(logging.Handler):
    """Custom logging handler to send logs to a centralized server."""

    def __init__(self, server_url: str):
        super().__init__()
        self.server_url = server_url

    def emit(self, record):
        log_entry = self.format(record)
        try:
            response = requests.post(self.server_url, json={"log": log_entry})
            response.raise_for_status()
        except requests.RequestException as e:
            # Handle exceptions (e.g., log to stderr or a fallback file)
            print(f"Failed to send log to server: {e}")


def set_logger(
    log_name: str,
    level: int = logging.WARNING,
    log_file_path: Path | None = None,
    server_url: str | None = None  # Add server_url parameter
) -> logging.Logger:
    """Create if needed and set a named logger with given log level, our standard format of:
        '2017-06-06:17:07:02,158 DEBUG    [log.py:11 in func()] This is a debug log'
        SUbsequent calls add another handler, so you can set different levels for say file and console logging,
        to say print errors to screen or whatever.
        Note calls only add handlers, and do not remove them - if this is wanted do so with the logging API

    Args:
        log_name (str): The name of logger. You can access it with: logging.getLogger(log_name).
                        The root logger is None, but it seems to inteact with other loggers so we do not allow it here
        level (int, optional): The logging level of:
            CRITICAL = 50, FATAL = CRITICAL, ERROR = 40, WARNING = 30, INFO = 20, DEBUG = 10
            Defaults to logging Warnings and above.
        log_file_path (Path | None, optional): The log file. Defaults to None (which goes to stderr).
        server_url (str | None, optional): The URL of the centralized logging server. Defaults to None.

    Returns:
        logging.Logger: The created logger
    """
    logger = logging.getLogger(log_name)
    min_level = level
    # logger level to minimum asked so messages get passed to handlers of all levels
    for handler in logger.handlers:
        min_level = min(handler.level, min_level)
    logger.setLevel(min_level)
    # set to stream stderr (default) or file
    if log_file_path is None:
        handler = logging.StreamHandler()  # stderr
    else:
        os.makedirs(log_file_path.parent, exist_ok=True)
        file_name = str(log_file_path.resolve())
        handler = logging.FileHandler(file_name)
    formatter = OneLineLogFormatter(LOG_FORMAT, LOG_DATE_FORMAT)
    handler.setFormatter(formatter)
    handler.setLevel(level)
    # If we need to edit handlers in future, we should perhaps set name of handles too so we can find them?
    logger.addHandler(handler)

    # Add centralized server handler if server_url is provided
    if server_url:
        server_handler = CentralizedServerHandler(server_url)
        server_handler.setFormatter(formatter)
        server_handler.setLevel(level)
        logger.addHandler(server_handler)

    return logger


def set_file_and_console_logger(
    log_name: str,
    log_file: Path,
    file_level: int = logging.INFO,
    console_level: int = logging.WARNING,
) -> logging.Logger:
    """Create and set a named file and console logger.
    Default console to show warnings+, and file to info, so we can see
    warnings and errors on screen, but log what we did in file.
    Default is to log INFO+ to file, and warnings+ to stderr.

    Args:
        log_name (str): the log name
        log_file (Path): This log file
        file_level (int, optional): The file logging level. Defaults to logging.INFO.
        console_level (int, optional): The conslole logging level. Defaults to logging.WARNING.

    Returns:
        logging.Logger: The logger
    """
    set_logger(log_name, file_level, log_file)
    logger = set_logger(log_name, console_level)
    return logger


# MONITORING_LOGGER = set_logger("monitoring_logger", logging.INFO, Path(gettempdir()) / "monitoring.log")
