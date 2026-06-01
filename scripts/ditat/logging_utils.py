"""Logging utilities."""

import logging


def setup_logging(verbose: bool = False) -> logging.Logger:
    """Configure root logging and return the 'ditat' logger."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    return logging.getLogger("ditat")
