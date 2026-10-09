@echo off
if defined MODELBUILDER_PYTHON (
  "%MODELBUILDER_PYTHON%" "%~dp0bootstrap.py" %*
) else (
  py -3.12 "%~dp0bootstrap.py" %*
)
exit /b %errorlevel%
