from setuptools import setup, find_packages

setup(
    name='apiDocsMiddleware',
    version='0.1.0',
    description='A lightweight middleware for API documentation (FastAPI and Flask)',
    author='Your Name',
    packages=find_packages(include=["health_checks", "health_checks.*"]),  # <-- Changed 'middlewares' to 'health_checks'
    install_requires=[
        "fastapi",
        "uvicorn",
        "starlette",
        "pyjwt",
        "flask",
        "httpx",
        "requests",
    ],
    python_requires=">=3.7",
)