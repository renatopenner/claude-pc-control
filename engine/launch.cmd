@echo off
rem pc-control MCP launcher: finds a Python 3 interpreter and starts the server.
setlocal
set "HERE=%~dp0"

where py >nul 2>nul
if %errorlevel%==0 (
  py -3 "%HERE%mcp_server.py"
  exit /b %errorlevel%
)

where python >nul 2>nul
if %errorlevel%==0 (
  python "%HERE%mcp_server.py"
  exit /b %errorlevel%
)

where python3 >nul 2>nul
if %errorlevel%==0 (
  python3 "%HERE%mcp_server.py"
  exit /b %errorlevel%
)

echo pc-control: Python 3 nao encontrado. Instale Python 3.10+ de python.org 1>&2
exit /b 9009
