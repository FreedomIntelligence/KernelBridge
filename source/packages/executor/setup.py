"""Explicit native package data; never dereference task or active-state links."""
import json
from pathlib import Path

from setuptools import setup

resources = json.loads((Path(__file__).parent / "native-resources.json").read_text())
setup(package_data={"kernelcat_executor": ["py.typed", *resources]})
