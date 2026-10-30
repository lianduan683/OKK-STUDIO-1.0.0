@echo off
setlocal
set "OKK_MCP_PYTHON=%~dp0.venv-mcp\Scripts\pythonw.exe"
if not exist "%OKK_MCP_PYTHON%" (
  echo Run the environment setup described in MCP.md first.
  pause
  exit /b 1
)
start "" "%OKK_MCP_PYTHON%" "%~dp0source\rtx5090_exporter.py" --mcp-bridge
