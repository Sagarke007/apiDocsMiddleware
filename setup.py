from setuptools import setup, find_packages

setup(
    name='apiDocsMiddleware',  # <-- Updated to match your new folder/project name
    version='0.1.0',
    description='A lightweight middleware for API documentation (FastAPI and Flask)',
    author='Your Name',  # <-- (optional) Put your name or organization
    packages=find_packages(include=["middlewares", "middlewares.*"]),
    install_requires=[
        "fastapi",
        "uvicorn",
        "starlette",
        "pyjwt",
        "flask",
    ],
    python_requires=">=3.7",
)
