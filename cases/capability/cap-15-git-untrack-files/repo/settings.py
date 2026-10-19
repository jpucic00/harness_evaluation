"""Reads settings from config.local.ini, falling back to config.example.ini."""

import configparser
from pathlib import Path


def load_settings(root: Path) -> configparser.ConfigParser:
    parser = configparser.ConfigParser()
    parser.read([root / "config.example.ini", root / "config.local.ini"])
    return parser
