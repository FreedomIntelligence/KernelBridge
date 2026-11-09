@echo off
if "%SCHOOL_SDK_PYTHON%"=="" (
  python "%~dp0socks_cmd.py" %1 %2
) else (
  "%SCHOOL_SDK_PYTHON%" "%~dp0socks_cmd.py" %1 %2
)
