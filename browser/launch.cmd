@echo off
rem browser-control MCP launcher: finds Python 3 and starts the CDP server.
setlocal
set "HERE=%~dp0"

where py >nul 2>nul
if %errorlevel%==0 (
  py -3 "%HERE%mcp_browser.py"
  exit /b %errorlevel%
)

where python >nul 2>nul
if %errorlevel%==0 (
  python "%HERE%mcp_browser.py"
  exit /b %errorlevel%
)

where python3 >nul 2>nul
if %errorlevel%==0 (
  python3 "%HERE%mcp_browser.py"
  exit /b %errorlevel%
)

echo browser-control: Python 3 nao encontrado. Instale Python 3.10+ de python.org 1>&2
exit /b 9009
