@echo off
if defined MODELBUILDER_PYTHON (
  "%MODELBUILDER_PYTHON%" "%~dp0..\distribution\smoke_ide.py" %*
) else (
  py -3 "%~dp0..\distribution\smoke_ide.py" %*
)
