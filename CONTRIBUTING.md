# Contributing to SovLens

First off, thank you for considering contributing to SovLens! It's people like you that make open-source software such a great community.

## Development Setup

SovLens has two main components:
1. The **Python FastAPI backend** (`backend/`) which handles AI inference (CLIP model), database operations (LanceDB), and media processing (PySceneDetect).
2. The **Tauri + Next.js frontend** (`frontend/`) which provides the glassmorphic native UI.

Please make sure you have read the `README.md` to set up both environments locally.

## Guidelines

### Code Style
- **Python:** We follow standard PEP 8 conventions. Please ensure your backend logic is heavily commented, especially around PyTorch hardware acceleration choices (CUDA/MPS fallback) and LanceDB queries.
- **Frontend:** Use React Functional Components and standard Hooks. For styling, strictly use Tailwind CSS utility classes. If adding new UI components, adhere to the "liquid glass" theme (no pure black/white, `#00b9a0` accent).

### Pull Requests
1. Fork the repository and create your branch from `main`.
2. If you've added code that should be tested, add tests.
3. Update the documentation (README.md) if you've changed functionality.
4. Ensure the test suite passes (if applicable).
5. Submit your PR with a clear description of the problem it solves and your proposed solution.

## Reporting Bugs
If you find a bug, please create an Issue on GitHub with:
- OS version and Hardware (specifically GPU info).
- Steps to reproduce.
- Any relevant logs from the Tauri console or the Python backend terminal.
