from __future__ import annotations

from setuptools import setup


def _read_requirements() -> list[str]:
    return [
        "openai>=1.0.0",
        "rich>=13.0.0",
        "click>=8.0.0",
        "prompt_toolkit>=3.0.0",
        "httpx>=0.24.0",
    ]


setup(
    name="code-one",
    version="0.1.0",
    description="Terminal-based agentic coding assistant CLI",
    python_requires=">=3.10",
    packages=["code_one"],
    install_requires=_read_requirements(),
    entry_points={
        "console_scripts": [
            "code-one=code_one.main:cli",
        ]
    },
    zip_safe=False,
)
